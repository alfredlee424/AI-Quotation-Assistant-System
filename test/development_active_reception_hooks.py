"""僅由測試 worker 注入存檔程序；非應用入口，不建立或偽造報告。"""


def active_barrier():
    os.write(ready_fd, b"R")
    if os.read(resume_fd, 1) != b"G":
        raise RuntimeError("test controller disconnected")


if phase == "active_inspection_read":
    # 只在原匯入前防護安裝之後注入；原命令入口、SQL、報告及退出碼不替換。
    active_marker = "raise SystemExit(development_inspection.main(sys.argv[2:]))"
    assert archive.runner._WORKER.count(active_marker) == 1
    active_setup = f"ready_fd = {ready_fd}\nresume_fd = {resume_fd}\n"
    archive.runner._WORKER = archive.runner._WORKER.replace(
        active_marker, active_setup + inspection_hooks + "\n" + active_marker)
    active_original_popen = archive.runner.subprocess.Popen

    def active_popen(*args, **kwargs):
        kwargs["pass_fds"] = (ready_fd, resume_fd)
        return active_original_popen(*args, **kwargs)

    archive.runner.subprocess.Popen = active_popen
elif phase == "active_before_run":
    active_original_run = archive.runner._run

    def active_run(*args):
        active_barrier()
        return active_original_run(*args)

    archive.runner._run = active_run
elif phase in ("active_before_link", "active_after_link"):
    active_original_link = os.link

    def active_link(*args, **kwargs):
        if phase == "active_before_link":
            active_barrier()
        result = active_original_link(*args, **kwargs)
        if phase == "active_after_link":
            active_barrier()
        return result

    os.link = active_link
elif phase == "active_after_receipt":
    active_original_stdout = sys.stdout

    class ActiveOutput:
        stopped = False

        def write(self, text):
            return active_original_stdout.write(text)

        def flush(self):
            active_original_stdout.flush()
            if not self.stopped:
                self.stopped = True
                active_barrier()

    sys.stdout = ActiveOutput()


active_original_main = archive.main


def active_main(*args):
    code = active_original_main(*args)
    # 這是原函式返回值，不冒充 OS wait 結果或接收紀錄；保持存活供父測試清理。
    os.write(ready_fd, b"D" + bytes([code]))
    if os.read(resume_fd, 1) != b"X":
        raise RuntimeError("test controller disconnected")
    return code


archive.main = active_main
