#!/bin/bash
# Drive the panel by hand, with the matrix library completely out of the way.
#
# Why this exists: every test so far has gone through rpi-rgb-led-matrix, so a
# failure could always be blamed on the library, the bindings, or our code. This
# talks to the HUB75 connector directly with pinctrl -- shift registers, latch,
# output-enable -- so whatever it shows is what the wire between the Pi's pads
# and the panel is actually doing.
#
# A 64x32 1/16-scan panel has two independent halves. R1/G1/B1 feed rows 0-15,
# R2/G2/B2 feed rows 16-31, and the A/B/C/D address lines pick which row of each
# half is lit. With the address at 0 that is row 0 (very top) and row 16 (the
# middle line). So each colour below should light TWO thin lines.
#
#   two lines  -> both halves receive that colour
#   one line   -> that colour's pin for the missing half is dead
#   no lines   -> neither, or the clock/latch/OE path is broken
#
# Usage:  sudo ./pin-probe.sh          (all three colours)
#         sudo ./pin-probe.sh red      (just one)

if ! command -v pinctrl >/dev/null 2>&1; then
  echo "pinctrl not found. On Bookworm/Trixie: sudo apt-get install -y raspi-utils"
  exit 1
fi

# Adafruit HAT / Bonnet pinout, straight out of the library's
# hardware-mapping.c so there is no chance of a transcription error.
OE=4; CLK=17; LAT=21
ADDR="22,26,27,20"          # A,B,C,D
R1=5;  G1=13; B1=6          # top half
R2=12; G2=16; B2=23         # bottom half
ALL_DATA="$R1,$G1,$B1,$R2,$G2,$B2"

P() { pinctrl set "$@" >/dev/null 2>&1; }

blank()   { P $OE op dh; }              # OE is active low: high = dark
show()    { P $OE op dl; }

shift_row() {                            # $1 = comma list of pins to hold high
  P $ALL_DATA op dl
  [ -n "$1" ] && P "$1" op dh
  for _ in $(seq 1 64); do
    P $CLK op dh
    P $CLK op dl
  done
  P $LAT op dh                           # latch the shifted row
  P $LAT op dl
}

cleanup() { blank; P $ALL_DATA op dl; P $CLK,$LAT op dl; }
trap cleanup EXIT INT TERM

echo "Putting every line into a known state."
P $OE,$CLK,$LAT op dl
P $ADDR op dl                            # address 0
P $ALL_DATA op dl
blank

run_one() {
  local name=$1 pins=$2 expect=$3
  echo
  echo "--- $name  (pins $pins) ---"
  echo "    expect: $expect"
  blank
  shift_row "$pins"
  P $ADDR op dl
  show
  sleep 6
  blank
  sleep 1
}

case "${1:-all}" in
  red)   run_one RED   "$R1,$R2" "a red line at the very top AND one across the middle" ;;
  green) run_one GREEN "$G1,$G2" "a green line at the very top AND one across the middle" ;;
  blue)  run_one BLUE  "$B1,$B2" "a blue line at the very top AND one across the middle" ;;
  top)
    # Every colour, top half only. If blue appears here and red/green do not,
    # GPIO 5 and GPIO 13 are not reaching the panel, full stop.
    run_one "TOP RED"   "$R1" "one red line at the very top, nothing in the middle"
    run_one "TOP GREEN" "$G1" "one green line at the very top, nothing in the middle"
    run_one "TOP BLUE"  "$B1" "one blue line at the very top, nothing in the middle"
    ;;
  *)
    run_one RED   "$R1,$R2" "a red line at the very top AND one across the middle"
    run_one GREEN "$G1,$G2" "a green line at the very top AND one across the middle"
    run_one BLUE  "$B1,$B2" "a blue line at the very top AND one across the middle"
    run_one WHITE "$ALL_DATA" "a white line at the very top AND one across the middle"
    ;;
esac

echo
echo "Done. Report, for each colour: two lines, one line (which?), or none."
