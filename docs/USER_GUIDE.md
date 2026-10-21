# Distributed Ray Trace 1.0.0 — User Guide

### Start Your Benchmark:

Click Start beside the mode you want to run. Open Settings to choose 160 × 90 or 640 × 360 before starting.

* CPU Local All Core — Uses all available logical CPU threads on this computer, subject to CPU affinity, memory headroom and a safety limit of 64 Workers.
* CPU Distributed All Core — Uses this computer's available logical CPU threads together with every selected, connected and compatible remote node. Each machine is limited to 64 Workers; the shared global limit is 256. The local participant starts automatically, so no separate local Agent or certificate is needed.
* CPU Serial Preview — Uses one Worker. Use this for a serial reference or a short functionality check.

The displayed result is an integer samples-per-second score. Local is the implicit 100% baseline; Distributed vs Local displays the signed percentage change for matching successful results. Exact Float64 matches are accepted; cross-platform Float64 differences can also compare when final RGB8 output and all ray counters match. Different workloads or outputs suppress comparison. Local ranking lists individual software Workers; Distributed ranking aggregates Local and each remote node. A Worker is an execution process, not a claim of fixed hardware-core affinity.

Click Cancel to stop a run, then wait for cleanup. A cancelled run does not produce a completed score. To test connectivity, connect nodes or use the zero-render probe below; a full image is unnecessary.

### Progress, Results and Local Cache

The fixed original Shogi room uses camera 5, 1 sample per pixel, depth 1 and seed 19. Settings selects 160 × 90 or 640 × 360. The initial reference image is labeled static. As tasks are accepted, real incomplete pixels and accepted-sample progress appear; a completed frame is displayed only after validation and resource cleanup.

Each mode retains its last successful score and contributions for the current application session. A later start, cancellation or failure does not erase that result. Scores reset when the application exits. Serial and Local contribution bars show each software Worker's accepted primary, continuation and shadow rays; Distributed shows Local and each remote machine. Counts exclude discarded work and are not hardware-core affinity measurements.

Cancel requests cooperative shutdown; wait until cleanup finishes before starting again. Do not remove active run files. Help → Clear Local Cache removes only the application's render/log cache after confirmation, when idle. It keeps settings, trust identities and assets and rejects unsafe filesystem links or busy files.

### How to Connect Remote Nodes: Prepare Identities

The standalone DRT_Linux_Backend_Release_1.0.0 package is for Linux x86_64 CLI use. It includes a native Linux executable, Python runtime, dependencies and the original room scene. Keep the complete directory together; do not rename a Windows .exe and try to run it on Linux. This build targets glibc 2.39 or newer (Ubuntu 24.04 / WSL Ubuntu 24.04); ARM and Alpine/musl require a separate build.

Extract the .tar.gz on the Linux filesystem, then read the built-in help. No Python installation is needed for the frozen package. The optional setup-pairing helper needs the system openssl command; the listening Agent does not.

```bash
tar -xzf DRT_Linux_Backend_Release_1.0.0-linux-x86_64.tar.gz
cd DRT_Linux_Backend_Release_1.0.0
./drt-agent --help
./drt-agent agent --help
./drt-agent doctor --json --check-spawn --check-image-codecs
```

Create a fresh pairing directory outside the release. For the first same-OS loopback test, use 127.0.0.1 for both addresses. Replace these addresses for LAN or WSL NAT as described below.

```bash
./drt-agent setup-pairing --output "$HOME/drt-pairing" \
  --node-host 127.0.0.1 --coordinator-host 127.0.0.1 \
  --node-id node-a --coordinator-id coordinator --port 45831
```

This creates separate node/ and coordinator/ folders containing fresh, certificate-pinned, mutually trusted identities. The test certificates expire in 30 days. Keep node/ and its private key on the node. Securely transfer coordinator/ to the desktop; it contains the desktop's private key, so do not post it or send it to unrelated nodes. For normal managed deployment, provision unique certificates using your trusted PKI instead. No credentials are included in the release.

### How to Connect Remote Nodes: Local Loopback

On Linux, the following creates an authenticated same-machine loopback connection and checks two health exchanges without starting numerical Workers or tracing rays. Keep the Agent running in the first terminal.

```bash
./drt-agent agent --config "$HOME/drt-pairing/node/agent.json"
```

In a second terminal, from the release directory:

```bash
./drt-agent configure-coordinator \
  --template "$HOME/drt-pairing/coordinator/topology.template.json"
./drt-agent probe --agents "$HOME/drt-pairing/coordinator/trusted.json"
```

Expect status PASS, authenticated true, compatible true, numerical_workers 0 and numerical_rays 0. The probe disconnects after checking; it does not leave a live GUI connection.

For an optional same-Windows-machine connectivity check, the integrated backend can run an Agent from the extracted Windows application directory:

```powershell
.\backend\drt-backend.exe agent --config 'C:\path\to\node\agent.json'
```

Use separately provisioned role identities for the listener and coordinator. The independent backend distribution remains Linux-only. Both ends must support the current room protocol; capability and resource verification reject incompatible peers.

On the Windows desktop, place coordinator/ in a private local directory. Run its configuration script there to resolve credential paths on this machine:

```powershell
& 'C:\path\to\coordinator\Configure-Coordinator.ps1'
```

Open the top Remote menu → Import trusted topology → choose coordinator/trusted.json. Select node-a, then click Discover / connect selected. Confirm Connected, authenticated and compatible before starting CPU Distributed All Core. Imported or selected alone does not mean connected. The coordinating Windows computer contributes automatically. After each completed, cancelled or failed run, the application restores selected idle connections so another test can start without clicking Connect again. Each execution still uses a fresh authenticated session. Explicit Disconnect stops this behavior; an unavailable node may require a later manual reconnect.

### How to Connect Remote Nodes: Same LAN

Choose a stable node IPv4 address reachable from the desktop and the desktop's source IPv4 address as observed by that node. For example, node 192.168.1.20 and desktop 192.168.1.10:

```bash
./drt-agent setup-pairing --output "$HOME/drt-lan-pairing" \
  --node-host 192.168.1.20 --coordinator-host 192.168.1.10 \
  --node-id linux-node --port 45831
./drt-agent agent --config "$HOME/drt-lan-pairing/node/agent.json"
```

Transfer coordinator/ securely to Windows, run Configure-Coordinator.ps1 there, then import trusted.json and connect in Remote. The Agent listens on the node; the desktop initiates the connection. No broadcast discovery, automatic trust or scene upload occurs. Allow only the chosen TCP port from the authorized desktop source address in your existing firewall policy; the program does not change firewall rules.

For additional nodes, use unique node IDs and keys, retain the same trusted coordinator identity, and extend agents[] in the desktop topology. Do not generate unrelated coordinator identities for each node and combine them under a single topology. Each node trusts the coordinator certificate and pin; the desktop trusts each node certificate and pin. Keep workers and global_workers as "auto" unless deliberately applying a lower administrative ceiling.

### How to Connect Remote Nodes: Linux CLI and WSL

For ordinary Linux, start the unpacked executable directly:

```bash
./drt-agent agent --config "$HOME/drt-lan-pairing/node/agent.json"
./drt-agent fingerprint --certificate "$HOME/drt-lan-pairing/node/node.crt"
```

Relative paths in agent.json resolve beside that configuration file; CLI path arguments resolve from the current directory. The bundled scene is selected automatically. To override it, pass --scene /absolute/path/shogi_match_room_v3.glb and, if needed, --resource-root /absolute/path. The original scene SHA256 must match the desktop; numerical policy, scene, trust or pin mismatches fail closed.

For WSL2 NAT, Windows and Linux do not necessarily observe the connection as 127.0.0.1. In WSL, use hostname -I to find the node IPv4 address and ip route show default to find the Windows-side gateway/source address. Start with explicit IPv4 addresses: set --node-host to the WSL address and --coordinator-host to the Windows source observed by WSL. If using mirrored networking or localhost forwarding, verify the actual source address rather than assuming it. WSL addresses can change after restarting; update the topology host and agent.json coordinator fields accordingly, without changing the certificate pins.

Use Remote on Windows to import the coordinator folder's locally generated trusted.json and connect to the WSL node. The CLI probe can also run from the extracted Windows release directory through its integrated backend:

```powershell
.\backend\drt-backend.exe probe --agents 'C:\path\to\coordinator\trusted.json' --scene 'C:\path\to\assets\shogi_match_room_v3.glb'
```

--help is available at the top level and for agent, setup-pairing, configure-coordinator, fingerprint, probe and doctor. The default Agent lifetime is 3600 seconds and the connection limit is 64; --lifetime-seconds and --max-sessions change these bounded values. Stop with Ctrl+C or SIGTERM. Agent startup and idle monitoring create no numerical Workers. --allow-remote-shutdown is off by default.

If connection fails, check addresses as seen by the peer, listener/port ownership, firewall policy, certificate expiry, the DER SHA256 pins, matching IDs, private-key permissions (Linux chmod 600), exact scene hash and Worker wire version 4. Check the Agent's startup/rejected messages and optional diagnostics. Do not disable TLS to work around a pairing error. WSL verification is a local cross-OS connection test; it does not certify a separate physical LAN.

### Need to uninstall?

Close the application and stop any Agents first. To remove Windows application data, delete:

```text
%LOCALAPPDATA%\DistributedRayTrace
```

This is C:\Users\[YOUR USER NAME]\AppData\Local\DistributedRayTrace on a standard Windows installation. Then delete the release directory you extracted (the current release folder). No fixed developer-machine path is required.

To remove only old renders and run logs, use Help → Clear Local Cache; it preserves settings, trust files and scene assets. On Linux, stop the Agent and remove its extracted release folder. Separately remove pairing/diagnostics directories only when no longer needed; protect or revoke private identities before discarding them.

### Seeking Help?

Hit me via email: yzho0092@uni.sydney.edu.au
