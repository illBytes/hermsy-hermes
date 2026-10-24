#!/usr/bin/env python3
"""The Hermsy observer for the Brief: the same snapshot, without the Chief
of Staff's budget and bookkeeping. (Cron scripts take no arguments.)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.argv.append("--brief")
from observe import main  # noqa: E402

main()
