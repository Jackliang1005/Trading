import subprocess


def send_feishu(target: str, message: str, silent: bool = False) -> bool:
    cmd = ["openclaw"]
    if silent:
        cmd.extend(["--log-level", "silent"])
    cmd.extend(["message", "send", "--channel", "feishu", "--target", target, "-m", message])
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0
