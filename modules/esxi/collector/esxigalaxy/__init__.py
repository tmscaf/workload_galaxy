"""ESXi Galaxy - a live map of a VMware ESXi host: every virtual machine, the networks and datastores
it hangs off, how full the host is, and what needs attention.

A module of the Workload Galaxy suite, built on galaxy_core. It reads the host through the vSphere
API with a read-only account and never changes anything.

Version comes from the module's VERSION file, baked into the image as ESXIGALAXY_VERSION with
ESXIGALAXY_BUILD (a UTC build stamp) by scripts/deploy-homelab.sh / the Dockerfile.
"""
import os

from galaxy_core import read_version

__version__ = read_version("ESXIGALAXY_VERSION", __file__)
__build__ = os.environ.get("ESXIGALAXY_BUILD", "local")
