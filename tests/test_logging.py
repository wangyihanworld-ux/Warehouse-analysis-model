from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from warehouse_analysis.logging_config import (
    LOG_FILE_NAME,
    configure_logging,
)


class LoggingConfigurationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        logger = logging.getLogger("warehouse_analysis")
        for handler in list(logger.handlers):
            if (
                isinstance(handler, logging.FileHandler)
                and Path(handler.baseFilename).parent == self.log_dir.resolve()
            ):
                handler.close()
                logger.removeHandler(handler)
        self.temp_dir.cleanup()

    def test_logging_initialization_creates_log_file(self) -> None:
        logger = configure_logging(self.log_dir)
        for handler in logger.handlers:
            handler.flush()
        self.assertTrue((self.log_dir / LOG_FILE_NAME).exists())

    def test_logging_writes_utf8_message(self) -> None:
        logger = configure_logging(self.log_dir)
        logger.info("测试运行步骤：FIFO计算完成")
        for handler in logger.handlers:
            handler.flush()
        content = (self.log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")
        self.assertIn("FIFO计算完成", content)
        self.assertIn("INFO", content)


if __name__ == "__main__":
    unittest.main()
