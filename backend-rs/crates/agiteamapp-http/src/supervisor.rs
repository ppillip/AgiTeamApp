//! ACT-POL-03 — PollerFaster-rs child process supervisor (방안 2: supervise).
//!
//! 검토메모_폴러백엔드통합_20260617.md §3.2 설계주의 이행:
//!  - env/인자 주입: --project-dir/--id-path/--team-bin/--workspace/--endpoint + collector token
//!  - restart policy: crash 시 exponential backoff. 설정오류(연속 즉시실패)는 무한재시작 억제(상한)
//!  - child stdout/stderr 를 백엔드 로그로 흡수(prefix `[poller]`)
//!  - shutdown: 백엔드 종료 시 child 에 SIGTERM 후 wait — orphan 금지
//!  - 중복 실행 방지: id_path lockfile(pid liveness) + child 에 AGITEAMAPP_POLLER_SUPERVISED=1 마커
//!  - 기동 순서: 서버 listen 후 child 기동(폴러 자체가 POST 실패를 다음 틱에 재시도)
//!  - on/off 토글: AGITEAMAPP_ACTIVITY_POLLER_MODE=supervised 일 때만 활성(끄면 현행 수동 실행)
//!
//! bin/PollerFaster-rs 바이너리 자체는 수정하지 않고 재사용한다(HTTP collect 계약 유지).

use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;

use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::watch;
use tokio::time::Instant;

/// 5초 미만 생존 후 종료 = 즉시실패(설정오류 추정 후보).
const FAST_FAIL_SECS: u64 = 5;
/// 연속 즉시실패가 이 횟수에 도달하면 무한재시작을 멈추고 stopped(설정오류 억제).
const MAX_FAST_FAILURES: u32 = 5;
/// 재시작 backoff 상한.
const MAX_BACKOFF_SECS: u64 = 30;
/// SIGTERM 후 child 종료를 기다리는 시간. 초과 시 SIGKILL.
const TERM_GRACE_SECS: u64 = 5;

/// supervisor 설정. backend main 이 env/기본값으로 채워서 넘긴다.
#[derive(Clone, Debug)]
pub struct SupervisorConfig {
    pub poller_bin: PathBuf,
    pub project_dir: PathBuf,
    pub id_path: PathBuf,
    pub team_bin: Option<PathBuf>,
    pub workspace: Option<String>,
    pub endpoint: String,
    pub collector_token: Option<String>,
    pub interval: Option<String>,
    pub lines: Option<String>,
    pub verbose: bool,
}

impl SupervisorConfig {
    /// env(+기본값)에서 설정을 구성. 활성(supervised 모드)이 아니면 None.
    ///
    /// - 토글: AGITEAMAPP_ACTIVITY_POLLER_MODE=supervised
    /// - project_dir = <projects_base>/<project_id>
    /// - poller_bin  = AGITEAMAPP_POLLER_BIN 또는 <project_dir>/bin/PollerFaster-rs
    /// - team_bin    = AGITEAMAPP_POLLER_TEAM_BIN 또는 <project_dir>/bin/team
    /// - id_path     = AGITEAMAPP_POLLER_ID_PATH 또는 <project_dir>/.agiteam/agiteam.id
    /// - endpoint    = http://127.0.0.1:<port>/api/webgui/internal/runtime-activity/collect
    pub fn from_env(
        projects_base: &str,
        project_id: &str,
        port: u16,
        collector_token: Option<String>,
    ) -> Option<Self> {
        let mode = std::env::var("AGITEAMAPP_ACTIVITY_POLLER_MODE")
            .unwrap_or_default()
            .to_lowercase();
        if mode != "supervised" {
            return None;
        }
        let project_dir = std::env::var("AGITEAMAPP_POLLER_PROJECT_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|_| {
                PathBuf::from(format!("{}/{}", projects_base.trim_end_matches('/'), project_id))
            });
        let poller_bin = std::env::var("AGITEAMAPP_POLLER_BIN")
            .map(PathBuf::from)
            .unwrap_or_else(|_| project_dir.join("bin").join("PollerFaster-rs"));
        let team_bin = std::env::var("AGITEAMAPP_POLLER_TEAM_BIN")
            .ok()
            .map(PathBuf::from)
            .or_else(|| Some(project_dir.join("bin").join("team")));
        let id_path = std::env::var("AGITEAMAPP_POLLER_ID_PATH")
            .map(PathBuf::from)
            .unwrap_or_else(|_| project_dir.join(".agiteam").join("agiteam.id"));
        let endpoint = std::env::var("AGITEAMAPP_POLLER_ENDPOINT").unwrap_or_else(|_| {
            format!("http://127.0.0.1:{port}/api/webgui/internal/runtime-activity/collect")
        });
        let workspace = std::env::var("AGITEAMAPP_POLLER_WORKSPACE")
            .ok()
            .filter(|s| !s.is_empty());
        let interval = std::env::var("AGITEAMAPP_POLLER_INTERVAL").ok().filter(|s| !s.is_empty());
        let lines = std::env::var("AGITEAMAPP_POLLER_LINES").ok().filter(|s| !s.is_empty());
        let verbose = matches!(
            std::env::var("AGITEAMAPP_POLLER_VERBOSE").unwrap_or_default().to_lowercase().as_str(),
            "1" | "true" | "yes"
        );

        Some(Self {
            poller_bin,
            project_dir,
            id_path,
            team_bin,
            workspace,
            endpoint,
            collector_token,
            interval,
            lines,
            verbose,
        })
    }

    fn build_command(&self) -> Command {
        let mut cmd = Command::new(&self.poller_bin);
        cmd.arg("--project-dir").arg(&self.project_dir);
        cmd.arg("--id-path").arg(&self.id_path);
        if let Some(team) = &self.team_bin {
            cmd.arg("--team-bin").arg(team);
        }
        if let Some(ws) = &self.workspace {
            cmd.arg("--workspace").arg(ws);
        }
        cmd.arg("--endpoint").arg(&self.endpoint);
        if let Some(iv) = &self.interval {
            cmd.arg("--interval").arg(iv);
        }
        if let Some(ln) = &self.lines {
            cmd.arg("--lines").arg(ln);
        }
        if self.verbose {
            cmd.arg("--verbose");
        }
        // child env: collector token 주입 + supervised 마커(중복 실행 식별).
        if let Some(tok) = &self.collector_token {
            cmd.env("AGITEAM_COLLECTOR_TOKEN", tok);
        }
        cmd.env("AGITEAMAPP_POLLER_SUPERVISED", "1");
        cmd.stdin(Stdio::null());
        cmd.stdout(Stdio::piped());
        cmd.stderr(Stdio::piped());
        // handle drop 시 자동 SIGKILL(orphan 안전망). 정상 종료 경로는 SIGTERM 우선.
        cmd.kill_on_drop(true);
        cmd
    }
}

/// id_path 기준 lockfile. drop 시 자동 해제.
struct PollerLock {
    path: PathBuf,
}

impl PollerLock {
    /// 동일 id_path 에 대한 supervised 폴러 중복 실행을 막는다.
    /// 기존 lock 의 pid 가 살아있으면 Err(점유), 죽었으면(stale) 회수 후 획득.
    fn acquire(id_path: &std::path::Path) -> Result<Self, String> {
        use sha2::{Digest, Sha256};
        let key = id_path.to_string_lossy();
        let digest = Sha256::digest(key.as_bytes());
        let short: String = digest.iter().take(8).map(|b| format!("{b:02x}")).collect();
        let path = std::env::temp_dir().join(format!("agiteamapp-poller-{short}.lock"));

        if let Ok(content) = std::fs::read_to_string(&path) {
            if let Ok(old_pid) = content.trim().parse::<i32>() {
                if pid_alive(old_pid) {
                    return Err(format!(
                        "id_path 에 대한 supervised 폴러가 이미 실행 중(pid={old_pid}, lock={})",
                        path.display()
                    ));
                }
            }
            // stale lock → 회수(아래에서 덮어씀).
        }
        let pid = std::process::id();
        std::fs::write(&path, pid.to_string())
            .map_err(|e| format!("lockfile 쓰기 실패 {}: {e}", path.display()))?;
        Ok(Self { path })
    }
}

impl Drop for PollerLock {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}

/// signal 0 으로 프로세스 생존 확인(POSIX). pid 가 살아있으면 true.
fn pid_alive(pid: i32) -> bool {
    if pid <= 0 {
        return false;
    }
    // kill(pid, 0): 0 또는 EPERM(존재하나 권한없음) → 생존. ESRCH → 없음.
    let rc = unsafe { libc::kill(pid as libc::pid_t, 0) };
    if rc == 0 {
        return true;
    }
    std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

/// child stdout/stderr 를 prefix 붙여 백엔드 로그로 흡수(운영자 가시성).
fn pipe_child_output(child: &mut Child) {
    if let Some(out) = child.stdout.take() {
        tokio::spawn(async move {
            let mut lines = BufReader::new(out).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                println!("[poller] {line}");
            }
        });
    }
    if let Some(err) = child.stderr.take() {
        tokio::spawn(async move {
            let mut lines = BufReader::new(err).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                eprintln!("[poller] {line}");
            }
        });
    }
}

/// child 에 SIGTERM 후 최대 TERM_GRACE_SECS 대기, 미종료 시 SIGKILL. orphan 금지.
async fn terminate_child(child: &mut Child) {
    if let Some(pid) = child.id() {
        unsafe {
            libc::kill(pid as libc::pid_t, libc::SIGTERM);
        }
        match tokio::time::timeout(Duration::from_secs(TERM_GRACE_SECS), child.wait()).await {
            Ok(_) => {
                eprintln!("[poller-supervisor] child(pid={pid}) SIGTERM 후 정상 종료");
                return;
            }
            Err(_) => {
                eprintln!(
                    "[poller-supervisor] child(pid={pid}) SIGTERM {TERM_GRACE_SECS}s 내 미종료 → SIGKILL"
                );
            }
        }
    }
    let _ = child.start_kill();
    let _ = child.wait().await;
}

/// supervisor 루프. shutdown_rx 가 true 가 되면 child 종료 후 반환한다.
/// 호출측은 listener bind 후 tokio::spawn 으로 띄우고, 서버 종료 시 watch 로 true 를 보낸다.
pub async fn run(cfg: SupervisorConfig, mut shutdown_rx: watch::Receiver<bool>) {
    if *shutdown_rx.borrow() {
        return;
    }
    if !cfg.poller_bin.exists() {
        eprintln!(
            "[poller-supervisor] 폴러 바이너리 없음 → supervisor 비활성: {}",
            cfg.poller_bin.display()
        );
        return;
    }
    let _lock = match PollerLock::acquire(&cfg.id_path) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("[poller-supervisor] 중복 실행 방지 — {e}. supervisor 비활성.");
            return;
        }
    };

    eprintln!(
        "[poller-supervisor] 활성: bin={} id_path={} endpoint={}",
        cfg.poller_bin.display(),
        cfg.id_path.display(),
        cfg.endpoint
    );

    let mut fast_failures: u32 = 0;
    let mut backoff_secs: u64 = 1;

    loop {
        if *shutdown_rx.borrow() {
            break;
        }
        let started = Instant::now();
        let mut child = match cfg.build_command().spawn() {
            Ok(c) => c,
            Err(e) => {
                eprintln!("[poller-supervisor] child spawn 실패: {e}");
                fast_failures += 1;
                if fast_failures >= MAX_FAST_FAILURES {
                    eprintln!(
                        "[poller-supervisor] 연속 즉시실패 {fast_failures}회(설정오류 추정) → supervisor 중단(stopped)"
                    );
                    break;
                }
                if !backoff_sleep(&mut shutdown_rx, &mut backoff_secs).await {
                    break;
                }
                continue;
            }
        };
        let pid = child.id().unwrap_or(0);
        eprintln!("[poller-supervisor] child 기동 pid={pid}");
        pipe_child_output(&mut child);

        // child 종료 또는 shutdown 중 먼저 오는 것 처리.
        let shutdown = tokio::select! {
            status = child.wait() => {
                let ran = started.elapsed().as_secs();
                match status {
                    Ok(st) => eprintln!(
                        "[poller-supervisor] child(pid={pid}) 종료 status={st} 생존={ran}s"
                    ),
                    Err(e) => eprintln!("[poller-supervisor] child(pid={pid}) wait 오류: {e}"),
                }
                false
            }
            _ = shutdown_rx.changed() => {
                if *shutdown_rx.borrow() {
                    eprintln!("[poller-supervisor] shutdown 수신 → child 종료 전파");
                    terminate_child(&mut child).await;
                    true
                } else {
                    false
                }
            }
        };
        if shutdown {
            break;
        }

        // restart policy: 즉시실패 누적 시 억제, 충분히 생존했으면 카운터·backoff 리셋.
        let ran = started.elapsed().as_secs();
        if ran < FAST_FAIL_SECS {
            fast_failures += 1;
            if fast_failures >= MAX_FAST_FAILURES {
                eprintln!(
                    "[poller-supervisor] 연속 즉시실패 {fast_failures}회(설정오류 추정: agiteam.id/team 부재 등) → supervisor 중단(stopped)"
                );
                break;
            }
        } else {
            fast_failures = 0;
            backoff_secs = 1;
        }

        if !backoff_sleep(&mut shutdown_rx, &mut backoff_secs).await {
            break;
        }
    }
    eprintln!("[poller-supervisor] 종료");
}

/// backoff 동안 sleep 하되 shutdown 이 오면 즉시 중단(false 반환 = 루프 종료).
/// sleep 후 backoff 를 지수 증가시킨다(상한 MAX_BACKOFF_SECS).
async fn backoff_sleep(shutdown_rx: &mut watch::Receiver<bool>, backoff_secs: &mut u64) -> bool {
    let wait = (*backoff_secs).min(MAX_BACKOFF_SECS);
    eprintln!("[poller-supervisor] {wait}s 후 재시작");
    let cont = tokio::select! {
        _ = tokio::time::sleep(Duration::from_secs(wait)) => true,
        _ = shutdown_rx.changed() => !*shutdown_rx.borrow(),
    };
    *backoff_secs = (*backoff_secs * 2).min(MAX_BACKOFF_SECS);
    cont
}
