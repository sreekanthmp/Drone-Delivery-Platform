"""Dispatch: choosing drones, planning and uploading missions, and flight commands.

The cloud never steers a drone (Rule 1): dispatch only picks a drone, uploads a
predefined mission and sends high-level commands (start, pause, return-to-home, land).
"""
