"""crewboss-runner (task #627, change #50, phase P4): the program on each PC that runs the Dashboard's jobs.

Wire protocol: docs/runner-protocol.md in the dashboard repo. HARD RULE (k98): this program never sends the PC's Claude login,
subscription token or API key to the Dashboard. The only secrets it sends are its own device token and signatures."""
__version__ = "0.1.0"
