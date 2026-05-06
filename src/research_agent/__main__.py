"""Allow ``python -m research_agent ...`` as an alternative to the ``research`` console script.

Useful when the console script entry point isn't on ``PATH`` — e.g., CI / verification
environments that shell out without ``uv tool install``-style binstubs.
"""

from research_agent.cli import app

if __name__ == "__main__":
    app()
