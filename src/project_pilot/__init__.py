"""project-pilot: a personal freelancermap.de listing pilot.

Fetches new project listings on a fixed interval, persists them losslessly,
evaluates fresh ones against a profile (hard rules then LLM), and reports
real matches as Claude match-thread sessions.
"""

__version__ = "0.1.0"
