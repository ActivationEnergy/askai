"""Private, versioned CLI-to-shell exchange; never expose it to subprocesses."""

import os
import stat

PROTOCOL = "2"
WRAPPER_FLAG = "--_askai-wrapper-v2"
SELECTED = 80
EXECUTED = 81
PRIVATE_ENV = ("ASKAI_COMMAND_FILE", "ASKAI_WRAPPER_PROTOCOL", "ASKAI_HOOK_INVOCATION")


def child_env(env=None):
    result = dict(os.environ if env is None else env)
    for key in PRIVATE_ENV:
        result.pop(key, None)
    return result


class CommandExchange:
    """Only the parent CLI can authorize an execution or report a child status."""

    def __init__(self):
        self.path = os.environ.get("ASKAI_COMMAND_FILE")
        self.hook = os.environ.get("ASKAI_HOOK_INVOCATION") == "1"
        self.executed = False
        self.committed = False
        self.fd = None

    def __enter__(self):
        from .core import AskError
        if self.path:
            if os.name != "posix":
                raise AskError("当前平台不支持此 Shell 集成协议")
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022:
                os.close(self.fd)
                self.fd = None
                raise AskError("Shell 执行交换文件不安全，未执行命令")
            os.ftruncate(self.fd, 0)
            if os.environ.get("ASKAI_WRAPPER_PROTOCOL") != PROTOCOL:
                os.close(self.fd)
                self.fd = None
                raise AskError("Shell 集成版本已更新，请重新打开终端，或重新加载 askai init 后再试")
        return self

    def write(self, text):
        from .core import AskError
        saved, current = os.fstat(self.fd), os.stat(self.path, follow_symlinks=False)
        if (saved.st_dev, saved.st_ino) != (current.st_dev, current.st_ino):
            raise AskError("Shell 执行交换文件已被替换，未授权执行")
        with os.fdopen(os.dup(self.fd), "w", encoding="utf-8") as output:
            output.seek(0)
            output.truncate()
            output.write(text)

    def select(self, command):
        self.write(command + "\n")
        self.committed = True
        return SELECTED

    def finish(self, status):
        if self.committed:
            return status
        if self.fd is not None and self.executed:
            self.write(str(status % 256) + "\n")
            self.committed = True
            return EXECUTED
        if self.hook and not self.executed and status == 0:
            return 127
        return status

    def __exit__(self, kind, value, tb):
        if self.fd is not None:
            try:
                if kind is not None or not self.committed:
                    os.ftruncate(self.fd, 0)
            finally:
                os.close(self.fd)
