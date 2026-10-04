# -*- coding: utf-8 -*-
"""sing-box 进程守护：启动 / 优雅重启 / 状态检测。

- ensure_running()：进程不在才拉起；已在跑（本进程或其他进程）则复用。
- reload(cfg)：校验 -> 停旧 -> 等端口释放 -> 起新 -> 确认真的起来了。
  起不来就抛异常，绝不"假装成功"。

为什么这么较真：停用一个用户，靠的就是"旧进程消失、新进程带着不含
他的配置跑起来"。如果重启静默失败，旧进程会带着旧口令继续服务，
停用就等于没做。所以 reload() 必须用真实证据（进程存活 + 端口在听）
确认切换成功，失败必须让调用方（Web API）返回错误。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import time
from pathlib import Path

import config
from converter import core

logger = logging.getLogger("distributor.supervisor")


class SupervisorError(RuntimeError):
    """sing-box 没能按预期起来/活着。"""


class Supervisor:
    # 重启时最多等端口释放多久（秒）
    PORT_RELEASE_TIMEOUT = 10.0
    # 拉起后等"进程存活且端口在听"多久（秒）
    STARTUP_TIMEOUT = 5.0

    def __init__(self, sing_box: Path | None = None):
        self.exe = sing_box or core.find_sing_box()
        self.proc: subprocess.Popen | None = None
        self._log_fh = None

    # ---------- 底层 ----------
    def _spawn(self, cfg_path: Path) -> subprocess.Popen:
        self._log_fh = open(config.RUN_LOG_PATH, "ab", buffering=0)
        env = os.environ.copy()
        proc = subprocess.Popen(
            [str(self.exe), "run", "-c", str(cfg_path)],
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with open(config.PIDFILE_PATH, "w") as f:
            f.write(str(proc.pid))
        logger.info("sing-box 已启动 pid=%s", proc.pid)
        return proc

    def _tail_log(self, n: int = 12) -> str:
        """取运行日志末尾几行 —— 启动失败时这是最有用的错误信息。"""
        try:
            text = config.RUN_LOG_PATH.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines[-n:])

    def _wait_until_serving(self, proc: subprocess.Popen, timeout: float) -> None:
        """确认新实例真的在服务：进程没退出 + 端口在听。

        两者缺一都算失败。之前不检查这一步，于是 "bind: address already
        in use" 这种秒退的失败会被当成成功返回，停用因此失效。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rc = proc.poll()
            if rc is not None:
                raise SupervisorError(
                    f"sing-box 启动后立即退出 (rc={rc})，配置未生效：\n{self._tail_log()}"
                )
            if _port_open(config.LISTEN_IP, config.LISTEN_PORT):
                return
            time.sleep(0.1)
        raise SupervisorError(
            f"sing-box 启动后 {timeout:.0f}s 内未监听 {config.LISTEN_IP}:{config.LISTEN_PORT}：\n{self._tail_log()}"
        )

    def _wait_port_free(self, timeout: float) -> None:
        """等旧实例把监听端口让出来，否则新实例必然 bind 失败。"""
        if not _port_open(config.LISTEN_IP, config.LISTEN_PORT):
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _port_open(config.LISTEN_IP, config.LISTEN_PORT):
                return
            time.sleep(0.1)
        raise SupervisorError(
            f"旧 sing-box 实例仍占用 {config.LISTEN_IP}:{config.LISTEN_PORT}，放弃重启"
        )

    def ensure_running(self, cfg_path: Path = config.CONFIG_PATH):
        """若进程不在则拉起。不主动重启正在跑的实例。"""
        if self._is_alive():
            return
        if not cfg_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {cfg_path}")
        self.proc = self._spawn(cfg_path)
        self._wait_until_serving(self.proc, self.STARTUP_TIMEOUT)

    def _is_alive(self) -> bool:
        """进程是否活着：先看本进程，再按 pidfile / 监听端口兜底。"""
        if self.proc is not None and self.proc.poll() is None:
            return True
        self.proc = None
        if _pid_alive(self._pid_from_file()):
            return True
        if _port_open(config.LISTEN_IP, config.LISTEN_PORT):
            return True
        return False

    def _pid_from_file(self) -> int | None:
        try:
            return int(config.PIDFILE_PATH.read_text().strip())
        except (OSError, ValueError):
            return None

    def _stop_any(self):
        """把正在跑的 sing-box 停掉（本进程或 pidfile 里的其他进程）。"""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        else:
            pid = self._pid_from_file()
            if pid and _pid_alive(pid):
                _kill_pid(pid)
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:
                pass
        self.proc = None
        try:
            config.PIDFILE_PATH.unlink()
        except OSError:
            pass

    def reload(self, cfg: dict, cfg_path: Path = config.CONFIG_PATH) -> None:
        """写入新配置 -> 校验 -> 停旧 -> 起新 -> 确认切换成功。

        失败抛 SupervisorError：此时旧实例已经被停掉，代理处于停止状态，
        调用方应把错误如实告诉操作者（而 Web 页仍可继续改配置后重载）。
        """
        core.validate(cfg)
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self._stop_any()
        self._wait_port_free(self.PORT_RELEASE_TIMEOUT)
        proc = self._spawn(cfg_path)
        self._wait_until_serving(proc, self.STARTUP_TIMEOUT)

    def stop(self):
        self._stop_any()


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        return False


def _kill_pid(pid: int):
    try:
        import psutil
        p = psutil.Process(pid)
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
    except Exception:
        pass


def _port_open(host: str, port: int) -> bool:
    """监听端口是否可连（0.0.0.0 用 127.0.0.1 探测）。"""
    ip = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    try:
        with socket.create_connection((ip, port), timeout=1.0):
            return True
    except OSError:
        return False
