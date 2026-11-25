"""Network Galaxy - a live map of a home or small-office network: every device, what it talks to
on the internet, what the firewall is turning away, and anything that looks abnormal.

A sibling of Workload Galaxy: it serves the same graph format to the same 3D front end, fed by a
pfSense router (ntopng for traffic, remote syslog for firewall and DHCP events).

Version comes from the repo's VERSION file, baked into the image as NETGALAXY_VERSION with
NETGALAXY_BUILD (a UTC build stamp) by tools/deploy-k3s.sh / the Dockerfile.
"""
import os
import pathlib


def _version() -> str:
    env = os.environ.get("NETGALAXY_VERSION", "")
    if env and env != "dev":
        return env
    here = pathlib.Path(__file__).resolve()
    for candidate in (here.parents[2] / "VERSION", here.parents[1] / "VERSION"):
        try:
            return candidate.read_text().strip()
        except OSError:
            continue
    return "0.0.0-dev"


__version__ = _version()
__build__ = os.environ.get("NETGALAXY_BUILD", "local")
