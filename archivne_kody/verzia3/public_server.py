"""
public_server.py — Verejný server pre Sedma Bere Tri

Spustenie:
    python public_server.py
    python public_server.py --port 65432
    python public_server.py --no-restart
"""

import subprocess
import sys
import time
import os
import argparse
import logging

LOG_FILE = "server.log"
RESTART_DELAY = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def run_server(port: int) -> int:
    env = os.environ.copy()
    env["PORT"] = str(port)
    logger.info(f"Spúšťam server na porte {port}...")
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(port)],
        env=env,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise


def main():
    parser = argparse.ArgumentParser(description="Sedma Bere Tri — verejný server s auto-reštartom")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", 65432)), help="Port servera")
    parser.add_argument("--no-restart", action="store_true", help="Vypnúť automatický reštart")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("  Sedma Bere Tri — Verejný server")
    logger.info(f"  Port: {args.port}  |  Auto-reštart: {not args.no_restart}")
    logger.info("=" * 60)

    crash_count = 0
    while True:
        try:
            exit_code = run_server(args.port)
            if exit_code == 0:
                logger.info("Server skončil čisto. Vypínam.")
                break
            crash_count += 1
            logger.warning(f"Server skončil s kódom {exit_code} (pád č. {crash_count})")
        except KeyboardInterrupt:
            logger.info("Vypnuté používateľom (Ctrl+C).")
            break
        except Exception as e:
            crash_count += 1
            logger.error(f"Neočakávaná chyba: {e} (pád č. {crash_count})")

        if args.no_restart:
            logger.info("Auto-reštart vypnutý. Ukončujem.")
            break

        logger.info(f"Reštartujem za {RESTART_DELAY}s...")
        time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    main()