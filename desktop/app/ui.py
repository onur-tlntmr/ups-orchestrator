import logging
import shutil
import subprocess
from config import UI_PROMPT_TIMEOUT

logger = logging.getLogger(__name__)


def run_cmd(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout.strip()


def notify(title: str, text: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", title, text], check=False)


def prompt_soft_suspend() -> str:
    title = "UPS Power Warning"
    text = "Power outage detected! What should the computer do?"

    timeout = UI_PROMPT_TIMEOUT

    logger.info(f"Prompting user for action (timeout={timeout}s)")

    if shutil.which("zenity"):
        logger.info("Using zenity for prompt")
        code, out = run_cmd(
            [
                "zenity",
                "--list",
                "--radiolist",
                "--title",
                title,
                "--text",
                text,
                "--column",
                "",
                "--column",
                "Option",
                "TRUE",
                "Shutdown",
                "FALSE",
                "Sleep",
                "--timeout",
                str(timeout),
            ]
        )

        if code == 0:
            choice = "cancel"
            if "Shutdown" in out:
                choice = "shutdown"
            elif "Sleep" in out:
                choice = "sleep"
            logger.info(f"User selected via zenity: {choice}")
            return choice

        if code == 1:  # Cancel button clicked or window closed
            logger.info("User dismissed the zenity dialog (Cancel/close). Returning 'cancel'.")
            return "cancel"

        if code == 5:  # Zenity timeout
            logger.warning(
                f"Zenity timed out after {timeout}s. Defaulting to shutdown."
            )
            return "shutdown"

        logger.error(
            f"Zenity failed with code {code}, output: {out!r}. Returning default 'shutdown' for safety."
        )
        return "shutdown"

    if shutil.which("kdialog"):
        code, out = run_cmd(
            [
                "kdialog",
                "--menu",
                text,
                "shutdown",
                "Shutdown",
                "sleep",
                "Sleep",
                "cancel",
                "Cancel",
                "--default",
                "shutdown",
                "--timeout",
                str(timeout),
            ]
        )

        if code == 0:
            val = out.strip()
            if val in {"shutdown", "sleep", "cancel"}:
                logger.info(f"User selected via kdialog: {val}")
                return val
            # If code is 0 but output is empty, it might be a timeout in kdialog
            logger.warning(
                "Kdialog returned 0 but empty selection? Defaulting to shutdown."
            )
            return "shutdown"

        logger.error(
            f"kdialog failed with code {code}, output: {out!r}. Returning default 'shutdown' for safety."
        )
        return "shutdown"

    logger.error(
        "No UI tool (zenity/kdialog) found. Notifying and returning default 'shutdown' for safety."
    )
    notify(title, text)
    return "shutdown"


def show_critical_warning() -> None:
    title = "UPS Critical Shutdown"
    text = "Battery level is critical. The computer will shut down now."

    if shutil.which("zenity"):
        subprocess.run(
            [
                "zenity",
                "--warning",
                "--title",
                title,
                "--text",
                text,
                "--timeout",
                str(UI_PROMPT_TIMEOUT),
            ],
            check=False,
        )
        return

    if shutil.which("kdialog"):
        subprocess.run(
            [
                "kdialog",
                "--sorry",
                text,
                "--title",
                title,
                "--timeout",
                str(UI_PROMPT_TIMEOUT),
            ],
            check=False,
        )
        return

    notify(title, text)
