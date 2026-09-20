# Installing from the SD card, with no terminal on the Pi

The Pi's Linux partition is ext4, which macOS cannot read or write. But the
*small* partition — the one Finder shows as `bootfs` — is plain FAT32, and
Pi OS reads a script from it on the very first boot. That is the whole trick:
drop the zip there, and the Pi installs itself.

## Making a card

1. Flash Raspberry Pi OS Lite with Raspberry Pi Imager. Choose 32-bit on a
   512 MB board (Zero 2 W, Pi 3 A+) and 64-bit on 1 GB or more (Pi 3 B+, Pi 4,
   Pi 5).

   Fill in the gear / "Edit settings" screen if you like. Then look at what
   landed on the card, because which files appear tells you which mechanism the
   image uses:

   ```
   ls /Volumes/bootfs | grep -iE 'user-data|meta-data|network-config|custom.toml|firstrun'
   ```

   | What you see | What the image is |
   | --- | --- |
   | `user-data`, `meta-data`, `network-config` | cloud-init: Pi OS Trixie, images from Nov 2025 on |
   | `custom.toml` | Pi OS Bookworm with Imager 1.8+ |
   | `firstrun.sh` | older Imager |
   | nothing | customisation did not apply |

   This matters because the mechanisms do not read each other's files. A
   `custom.toml` on a Trixie image is simply ignored, and it stays on the card
   afterwards rather than being consumed, which is the giveaway. The prep script
   detects which one the card wants (`ds=nocloud` in `cmdline.txt`) and writes
   the matching format.

   Imager also has a known intermittent bug where the settings never reach the
   card at all. Update Imager, but do not depend on it: step 2 writes them
   itself.

2. Leave the card plugged into the Mac. The prep script ships inside the
   release, so take it out of there rather than keeping a loose copy:

   ```
   cd ~/Downloads
   unzip -o led-scoreboard-v2.18.zip 'scoreboard/boot/*'
   chmod +x scoreboard/boot/prepare-sd-card.sh

   scoreboard/boot/prepare-sd-card.sh ~/Downloads/led-scoreboard-v2.18.zip \
     --name "Living Room Scoreboard" \
     --panels 2 \
     --wifi "YourNetwork" --wifi-pass "…" \
     --user yourname --user-pass "…"
   ```

   The two paths on the first line of that last command are doing different
   jobs: the first is the script being run, the second is the release it copies
   onto the card. Neither needs to be on the card already, and nothing gets
   dragged in by hand.

   The `--wifi` and `--user` options replace Imager's gear screen, writing
   whichever format the card wants: cloud-init's three files on a Trixie image,
   `custom.toml` on an older one. Same files Imager would have produced, written
   once and checked rather than left to a race. They also always enable SSH and
   grant the account passwordless sudo, both of which the installer needs and
   neither of which is obvious by its absence.

   Passing them wins over anything Imager left on the card: that file is moved
   aside to `custom.toml.imager-bak` rather than deleted. Leave the options off
   and Imager's settings stand untouched.

   The login password is hashed if your `openssl` supports it and plain text if
   not, and the script says which. The wifi password is always plain, because
   the encrypted form is reported not to apply reliably. FAT partitions have no
   file permissions, so delete `custom.toml` from the card after the first boot
   if that matters.

   Give each board a name of its own. Two boards both called `Scoreboard` both
   want `scoreboard.local`, and on one network only one of them gets it. The
   name also sets the hostname in `custom.toml`, so the board answers at the
   right `.local` address from its very first boot.

3. Eject, put the card in the Pi, connect the panel, power on.

## What happens on the Pi

| Boot | Takes | What it does |
| --- | --- | --- |
| First | ~1 min | Copies the zip off the card, arms the installer, reboots |
| Second | 5–20 min | Waits for wifi, unpacks, runs `install.sh --yes`, starts the board |

The panel lights up when it is done. The board is then at
`http://living-room-scoreboard.local:8080`.

Two boots rather than one is deliberate. The first-boot hook runs in a
stripped-down systemd target with no networking, which is fine for copying a
file and far too early to fetch packages. The second boot has a real network
stack, so the install is a normal `After=network-online.target` service.

## When it does not work

Everything is logged to **`scoreboard-install.log` on the card itself**, so
put the card back in the Mac and open it. That is the only diagnostic that
works with no screen, no keyboard and no ssh.

### On the Mac, before the card is ever booted

- *`zsh: no such file or directory`* — that is your Mac, not the Pi. `~/` is
  only shorthand for your home folder, so the shell is reporting that the
  script is not at the path you gave. Take it out of the zip as in step 2
  instead of hunting for a stray download.
- *macOS refuses to run it* — a script downloaded through a browser is
  quarantined. Run it as an argument to bash:
  `bash scoreboard/boot/prepare-sd-card.sh ...`
- *"this card has no wifi and no user account"* — usually Imager's
  intermittent bug rather than anything you did. Nothing was written to the
  card, so do not re-flash and hope: re-run with `--wifi`, `--wifi-pass`,
  `--user` and `--user-pass` and let the script write `custom.toml` itself.

### On the Pi, read from the log

- *"no usable account"* — the card reached the Pi with no login on it. Re-run
  the prep script with `--user` and `--wifi`, then re-flash: the customisation
  files are only read on a first boot.
- *`target user:` names an account you did not ask for* — you are on a build
  from before this was fixed. Stage A used to resolve the user before
  cloud-init had created it, and got the image's placeholder.
- *"no network after 5 minutes"* — the wifi in Imager was wrong. Fix it, ssh
  in, and run `~/scoreboard/install.sh --yes`. Nothing is lost; the payload is
  already on the Pi.
- *Log stops partway* — a step failed. `install.sh` reports each failure by
  name and keeps going, so the tail of the log names the step to retry.

## Files this puts on the card

| File | Purpose |
| --- | --- |
| `led-scoreboard-*.zip` | the payload |
| `scoreboard-firstboot.sh` | stage A, run by `systemd.run=` |
| `scoreboard-name.txt` | optional, one line, sets the board's name and hostname |
| `scoreboard-panels.txt` | optional, one line, how many panels are chained left to right |
| `custom.toml` | wifi and login on Bookworm images |
| `user-data`, `meta-data`, `network-config` | wifi and login on Trixie (cloud-init) images |
| `scoreboard-no-comitup` | optional, empty file, skips the wifi captive portal |
| `scoreboard-install.log` | written by the Pi, read by you |

All of them can be deleted afterwards; the Pi does not need them again.
