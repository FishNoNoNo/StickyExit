# -*- coding: utf-8 -*-
"""sing-box 进程守护：启动 / 优雅重启 / 状态检测。

- ensure_running()：确保有一个"跑着当前配置"的实例。没在跑就拉起；在跑但
  跑的不是当前配置就重启接管。判据是配置内容摘要，不是"有没有进程在"。
- reload(cfg)：校验 -> 停旧 -> 等端口释放 -> 起新 -> 确认真的起来了。
  起不来就抛异常，绝不"假装成功"。

为什么这么较真：停用一个用户，靠的就是"旧进程消失、新进程带着不含
他的配置跑起来"。如果重启静默失败，旧进程会带着旧口令继续服务，
停用就等于没做。所以 reload() 必须用真实证据（进程存活 + 端口在听）
确认切换成功，失败必须让调用方（Web API）返回错误。
"""
from __future__ import annotations

import hashlib
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


def _file_digest(path: Path) -> str | None:
    """配置文件内容的摘要；读不到返回 None。"""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


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
    def _remember_running(self, cfg_path: Path) -> None:
        """记下"这个实例跑的是哪份配置"，供下次启动比对。"""
        digest = _file_digest(cfg_path)
        if not digest:
            return
        try:
            config.CONFIG_HASH_PATH.write_text(digest, encoding="utf-8")
        except OSError:
            # 记不下来不影响本次运行，只是下次启动会保守地重启一次。
            logger.warning("无法记录配置摘要，下次启动将重启 sing-box")

    def _running_digest(self) -> str | None:
        """在跑的实例所用的配置摘要；没有记录返回 None。

        返回 None 时的比对必然不等 -> 重启接管，这是安全方向：
        旧版本留下的实例没有这个记录，我们无从判断它跑的是什么。
        """
        try:
            return config.CONFIG_HASH_PATH.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _forget_running(self) -> None:
        try:
            config.CONFIG_HASH_PATH.unlink()
        except OSError:
            pass

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
        self._remember_running(cfg_path)
        logger.info("sing-box 已启动 pid=%s", proc.pid)
        return proc

    def _start(self, cfg_path: Path) -> None:
        """拉起实例并确认真的在服务；没起来就别留下"它跑着"的记录。

        摘要早写一步会让"启动失败"和"跑着旧配置"在下次启动时难以区分，
        所以失败路径必须清掉。
        """
        proc = self._spawn(cfg_path)
        try:
            self._wait_until_serving(proc, self.STARTUP_TIMEOUT)
        except Exception:
            self._forget_running()
            raise

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

        两者缺一都算失败：漏判会让 "bind: address already in use" 这种秒退的
        失败被当成成功返回，停用因此失效。
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
        """确保有一个"跑着这份配置"的实例。

        - 没在跑 -> 拉起。
        - 在跑但跑的不是这份配置 -> 重启接管。

        为什么"有进程在"不算数：sing-box 是 python 的子进程，python 被强杀
        （或机器断电）后它会被孤儿化，继续用旧配置服务 —— 里面还留着上次
        会话里已经停用的用户和已改过的旧口令，等于停用/改密全部失效。
        所以判据必须是"这个实例跑的配置 == 当前配置"，靠内容摘要比对。
        """
        if self._is_alive():
            if self._running_digest() == _file_digest(cfg_path):
                logger.info("复用正在运行的 sing-box 实例（与当前配置一致）")
                return
            logger.warning("在跑的 sing-box 与当前配置不一致，重启以应用当前配置")
            self.reload(self._read_config(cfg_path), cfg_path)
            return
        if not cfg_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {cfg_path}")
        self._start(cfg_path)

    @staticmethod
    def _read_config(cfg_path: Path) -> dict:
        return json.loads(cfg_path.read_text(encoding="utf-8"))

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
        # 实例已经不在，"它跑着哪份配置"的记录必须一起清掉：
        # 留着会让下次启动误判为"跑的是当前配置"而复用一个不存在的实例。
        self._forget_running()

    def reload(self, cfg: dict, cfg_path: Path = config.CONFIG_PATH) -> None:
        """写入新配置 -> 校验 -> 停旧 -> 起新 -> 确认切换成功。

        失败抛 SupervisorError：此时旧实例已经被停掉，代理处于停止状态，
        调用方应把错误如实告诉操作者（而 Web 页仍可继续改配置后重载）。
        """
        core.validate(cfg)
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self._stop_any()
        self._wait_port_free(self.PORT_RELEASE_TIMEOUT)
        self._start(cfg_path)

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
