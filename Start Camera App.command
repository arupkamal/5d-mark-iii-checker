#!/bin/bash
# Double-click this file in Finder to start the 5D Mark III app.
cd "$(dirname "$0")"
exec .venv/bin/python camera_check.py
