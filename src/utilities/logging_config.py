"""
Logging utility configuration
"""

import logging
import sys
from pathlib import Path
from datetime import datetime

def setupLogger(name: str, logLevel: int = logging.INFO) -> logging.Logger:
    """Setup logger with file and console handlers"""

    logger = logging.getLogger(name)
    logger.setLevel(logLevel)

    if logger.handlers:
        return logger

    logDir = Path("logs")
    logDir.mkdir(exist_ok=True)

    fileHandler = logging.FileHandler(
        logDir / f"{datetime.now().strftime('%Y%m%d')}.log"
    )
    fileHandler.setLevel(logLevel)

    consoleHandler = logging.StreamHandler(sys.stdout)
    consoleHandler.setLevel(logLevel)

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    fileHandler.setFormatter(formatter)
    consoleHandler.setFormatter(formatter)

    logger.addHandler(fileHandler)
    logger.addHandler(consoleHandler)

    return logger
