import logging
import os
import platform
import sys


def setup_logger():
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(stream=sys.stdout if sys.stdout is not None else open(os.devnull, 'w'))],
        force=True,
    )

    logging.info("=== SESSION STARTED ===")
    logging.info(f"OS: {platform.system()} {platform.release()}")
    logging.info(f"Python: {sys.version}")

    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logging.critical("Unhandled exception:", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = handle_exception
