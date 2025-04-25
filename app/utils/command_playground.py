import subprocess
import logging

def execute_command(command):
    """
    Executes a shell command in a controlled environment.

    Args:
        command (str): The command to execute.

    Returns:
        dict: A dictionary containing the command's output, error, and exit code.
    """
    try:
        result = subprocess.run(
            command, shell=True, text=True, capture_output=True, timeout=30
        )
        return {
            "success": True,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired as e:
        logging.error(f"Command timed out: {command}")
        return {
            "success": False,
            "stdout": "",
            "stderr": str(e),
            "exit_code": -1,
        }
    except Exception as e:
        logging.error(f"Error executing command: {command}, Error: {e}")
        return {
            "success": False,
            "stdout": "",
            "stderr": str(e),
            "exit_code": -1,
        }