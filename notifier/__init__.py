# -*- coding: utf-8 -*-
"""
notifier — Phase 2 delivery: the only component that talks to Telegram.

    notifier/telegram.py   the single Telegram Bot API sender
                           (claim → gate → cap → send → mark)

Cloud-side only. No pyodbc, no adapter_hdsoft, no SQL Server connection
anywhere in this package (enforced mechanically by test_notifier.py).
"""
