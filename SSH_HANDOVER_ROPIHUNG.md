# Handover Guide: `ropihung` SSH Access & Tapo P110M Smart Home Project

This document provides everything needed to transfer SSH access for the **`ropihung`** Raspberry Pi and manage the **Tapo A → Tapo B** smart home controller.

---

## 1. Hardware & Network Inventory

### Raspberry Pi Controller (`ropihung`)
| Property | Value |
| :--- | :--- |
| **Hostname** | `ropihung` (mDNS: `ropihung.local`) |
| **Current LAN IP** | `192.168.31.217` (`192.168.31.0/24`) |
| **MAC Address** | `88:A2:9E:F0:91:30` |
| **OS / Architecture** | Debian GNU/Linux (`6.18.50+rpt-rpi-2712`, `aarch64`) |
| **SSH Username** | `ropi` |
| **SSH Port** | `22` |

### Smart Plugs Controlled by `ropihung`
| Role | Tapo App Nickname | Model | Current IP | MAC Address | Device ID |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Tapo A** (TV Current Sensor) | `WSC1F Tapo A P110M` | `P110M(US)` | `192.168.31.9` | `EC-B9-31-25-07-E7` | `703641078388d1ea1b197481d1cbbae9` |
| **Tapo B** (Bulb Switch) | `WSC Tapo B P110M` | `P110M(US)` | `192.168.31.136` | `EC-B9-31-24-FD-FF` | `0f8ba32d18c577b38c27f77031e37e2d` |

---

## 2. Transferring SSH Access to Your Friend

### Step 1 — On Your Friend's Computer: Generate an SSH Key (if needed)
```bash
ssh-keygen -t ed25519 -C "friend@smart-home"
cat ~/.ssh/id_ed25519.pub
```

### Step 2 — Authorize the Key on `ropihung`
Either **you** (from your Mac before handover) or **your friend** (using the `ropi` user password) can add their public key to `ropihung`:

* **If your friend runs it from their own computer** (requires `ropi` password once):
  ```bash
  ssh-copy-id -i ~/.ssh/id_ed25519.pub ropi@ropihung.local
  ```
  *(Or use the IP address: `ssh-copy-id -i ~/.ssh/id_ed25519.pub ropi@192.168.31.217`)*

* **If you add your friend's public key from your Mac** (no password required):
  ```bash
  ssh ropihung "echo 'PASTE_FRIENDS_PUBLIC_KEY_HERE' >> ~/.ssh/authorized_keys"
  ```

### Step 3 — Configure the `ssh ropihung` Shortcut on Your Friend's Computer
Have your friend add the following block to `~/.ssh/config` on their computer:

```sshconfig
Host ropihung
    HostName ropihung.local
    # Fallback static/DHCP IP on current router: 192.168.31.217
    User ropi
    IdentityFile ~/.ssh/id_ed25519
    StrictHostKeyChecking accept-new
```

Set proper permissions and test the connection:
```bash
chmod 600 ~/.ssh/config
ssh ropihung
```

### Step 4 — (Optional) Remove Your Old Mac's Key After Handover
Once your friend confirms `ssh ropihung` works from their machine, remove your old Mac's key from `~/.ssh/authorized_keys` on `ropihung`:
```bash
ssh ropihung "sed -i '/rolandtalkonmini@rolandtalkdeMac-mini.local/d' ~/.ssh/authorized_keys"
```

---

## 3. Managing the Tapo Smart Home Application on `ropihung`

### File Locations on `ropihung`
* **Application Directory**: `/home/ropi/tvlighton/`
  * `tapo_klap.py` — Zero-dependency local `KLAP v2` protocol & UDP port `20002` discovery module.
  * `tvlighton.py` — Main automation daemon & CLI utility.
  * `config.json` — Credentials (`chmod 600`), device MAC/IP bindings, and power thresholds.
* **Systemd User Service**: `/home/ropi/.config/systemd/user/tvlighton.service`

---

### Common Operations (Run via `ssh ropihung`)

#### 1. Check Service Status
```bash
ssh ropihung "systemctl --user status tvlighton.service"
```

#### 2. Restart / Stop / Start the Automation Daemon
```bash
# Restart after changing config.json
ssh ropihung "systemctl --user restart tvlighton.service"

# Stop the automation temporarily
ssh ropihung "systemctl --user stop tvlighton.service"

# Start the automation
ssh ropihung "systemctl --user start tvlighton.service"
```

#### 3. Check Live Power Draw (`Watts`) & Relay State of Tapo A & Tapo B
```bash
ssh ropihung "python3 /home/ropi/tvlighton/tvlighton.py --status"
```

#### 4. Manually Toggle Tapo B (Bulb) On or Off
```bash
ssh ropihung "python3 /home/ropi/tvlighton/tvlighton.py --bulb on"
ssh ropihung "python3 /home/ropi/tvlighton/tvlighton.py --bulb off"
```

#### 5. Re-Scan the Local Network for Tapo Plugs
```bash
ssh ropihung "python3 /home/ropi/tvlighton/tvlighton.py --discover"
```

---

## 4. Changing Thresholds, Wi-Fi Networks, or Tapo Accounts

If the plugs or Raspberry Pi are moved to a new Wi-Fi subnet or a new Tapo account:

1. Edit `/home/ropi/tvlighton/config.json` on `ropihung`:
   ```bash
   ssh ropihung "nano /home/ropi/tvlighton/config.json"
   ```
2. Key settings inside `config.json`:
   * `tapo_credentials.email` / `tapo_credentials.password`: TP-Link Tapo account login (Ensure **Third-Party Compatibility** is enabled in the Tapo app under *Me → Third-Party Services*).
   * `automation.subnet_prefix`: First three octets of the LAN subnet (e.g. `"192.168.31"`). Even if DHCP assigns new IPs to Tapo A or Tapo B, the controller automatically finds their new IPs using their MAC addresses (`EC-B9-31-25-07-E7` and `EC-B9-31-24-FD-FF`).
   * `automation.on_threshold_watts`: Wattage on Tapo A that triggers Tapo B **ON** (currently `10.0` W).
   * `automation.off_threshold_watts`: Wattage on Tapo A that triggers Tapo B **OFF** (currently `5.0` W).
3. Restart the service to apply changes:
   ```bash
   ssh ropihung "systemctl --user restart tvlighton.service"
   ```
