"""Config web UI, served from the Pi.

The point is to never SSH in to change a favourite team again. Everything the
rotation cares about is editable from a phone, and changes take effect on the
next frame because the render loop watches config.version rather than
restarting the process.
"""

import io
import json
import os
import time

from flask import (Flask, jsonify, redirect, render_template, request,
                   send_file, url_for)

from data import fantasy, geocode, hardware, identity, leagues, secrets, slate
from renderer import display as display_mod


def _version():
    """The running version, read from main.py rather than imported.

    Importing main would start the board a second time inside the web process.
    """
    import re
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "main.py")
    try:
        with open(path) as fh:
            for line in fh:
                found = re.match(r'VERSION\s*=\s*"([^"]+)"', line)
                if found:
                    return found.group(1)
    except Exception:
        pass
    return ""


def _update_state():
    """What the self-updater last did, for the settings page and status.

    Written by tools/selfupdate.sh, which runs as its own systemd unit, so this
    is the only channel between the two. A board that has never checked returns
    an empty dict rather than an error: nothing has gone wrong, it is just new.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "state", "update-status.json")
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _display_tz(config):
    """The configured display timezone, or None for system local."""
    try:
        from renderer.screens.base import resolve_tz
        return resolve_tz(config.get("display.timezone", ""))
    except Exception:
        return None


def create_app(config, store):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["JSON_SORT_KEYS"] = False

    @app.after_request
    def no_cache(resp):
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---------------------------------------------------------------- pages

    @app.route("/")
    def index():
        # A board that has never been set up opens the wizard instead of the
        # settings page. This is the first thing a brother sees.
        if not config.get("setup.complete"):
            return redirect(url_for("setup"))
        return render_template("index.html",
                               board_name=config.get("identity.name", "Scoreboard"),
                               mdns=identity.mdns_name())

    @app.route("/fantasy")
    def fantasy_page():
        return render_template("fantasy.html")

    @app.route("/setup")
    def setup():
        return render_template("setup.html",
                               board_name=config.get("identity.name", ""),
                               zip_code=config.get("location.zip", ""))

    # ---------------------------------------------------------------- api

    @app.route("/api/config", methods=["GET"])
    def get_config():
        return jsonify({
            "config": config.data,
            "leagues": [
                {"key": key, "label": league.label, "ranked": league.ranked,
                 "has_rankings": bool(league.rankings_path),
                 "has_standings": bool(league.standings_path)}
                for key, league in leagues.LEAGUES.items()
            ],
        })

    @app.route("/api/config", methods=["POST"])
    def post_config():
        payload = request.get_json(silent=True) or {}
        try:
            config.replace(payload)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        # Favourites or leagues may have changed, so refetch immediately
        # instead of waiting out the idle interval.
        store.wake()
        return jsonify({"ok": True, "version": config.version})

    @app.route("/api/hardware")
    def hardware_info():
        """What board this is and what tuning it resolved to."""
        return jsonify(hardware.summary(config))

    @app.route("/api/status")
    def status():
        games = store.snapshot()
        return jsonify({
            "online": store.online,
            "error": store.error,
            "stale_seconds": round(store.stale_seconds(), 1),
            "generation": store.generation,
            "counts": {
                "total": len(games),
                "live": sum(1 for g in games if g.live),
                "favorites": sum(1 for g in games if g.is_favorite),
            },
            "version": _version(),
            "update": _update_state(),
            "current_screen": display_mod.LATEST.get("key"),
            # Which day's games the rotation is treating as current, and which
            # leagues that leaves in. Without this, "why is college not
            # showing" has no answer anywhere the owner can see.
            "slate": slate.describe(games, _display_tz(config)),
            "games": [
                {
                    "league": g.league_label,
                    "away": g.away.ranked_short, "away_score": g.away.score,
                    "home": g.home.ranked_short, "home_score": g.home.score,
                    "state": g.state,
                    "detail": g.detail,
                    "favorite": g.is_favorite,
                }
                for g in games[:40]
            ],
        })

    @app.route("/api/teams/<league_key>")
    def teams(league_key):
        """Abbreviations seen in the current slate, to populate the picker.

        Deliberately derived from live data rather than a hardcoded table:
        conference realignment keeps breaking hardcoded tables.
        """
        seen = {}
        for game in store.snapshot():
            if game.league != league_key:
                continue
            for team in game.teams:
                if team.abbr:
                    seen[team.abbr] = team.display or team.location
        extra = store.get_rankings(league_key)
        for entry in extra:
            seen.setdefault(entry.abbr, entry.name)
        return jsonify(sorted(
            ({"abbr": k, "name": v} for k, v in seen.items()),
            key=lambda t: t["abbr"]))

    @app.route("/api/preview.png")
    def preview():
        image = display_mod.LATEST.get("image")
        if image is None:
            return "", 204
        scale = min(12, max(1, int(request.args.get("scale", 8))))
        big = image.resize((image.width * scale, image.height * scale),
                           resample=0)          # nearest neighbour, keep pixels
        buf = io.BytesIO()
        big.save(buf, format="PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png")

    @app.route("/api/refresh", methods=["POST"])
    def refresh():
        store.wake()
        return jsonify({"ok": True})

    # ---------------------------------------------------------- location

    @app.route("/api/zip/<zip_code>")
    def zip_lookup(zip_code):
        """Live validation while they type, so a typo is caught immediately."""
        try:
            location = geocode.lookup(zip_code)
        except geocode.GeocodeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        return jsonify({
            "ok": True, "zip": location.zip, "city": location.city,
            "state": location.state, "label": location.label,
            "latitude": location.latitude, "longitude": location.longitude,
        })

    # ---------------------------------------------------------- identity

    @app.route("/api/identity", methods=["GET"])
    def get_identity():
        pending = identity.pending_rename()
        return jsonify({
            "name": config.get("identity.name", "Scoreboard"),
            "hostname": identity.current_hostname(),
            "mdns": identity.mdns_name(),
            "pending_hostname": pending,
            "preview": identity.slugify(config.get("identity.name", "")),
        })

    @app.route("/api/identity", methods=["POST"])
    def post_identity():
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name") or "").strip()[:40]
        if not name:
            return jsonify({"ok": False, "error": "name is required"}), 400

        hostname = identity.slugify(name)
        config.set("identity.name", name)
        config.set("identity.hostname", hostname)
        config.save()

        # Queued rather than applied: the render process has dropped
        # privileges by now and cannot call hostnamectl itself.
        changed = hostname != identity.current_hostname()
        if changed:
            identity.request_rename(hostname)

        return jsonify({
            "ok": True, "name": name, "hostname": hostname,
            "mdns": "{}.local".format(hostname),
            "reboot_required": changed,
        })

    # ---------------------------------------------------------- setup

    @app.route("/api/setup", methods=["POST"])
    def post_setup():
        """One-shot first-run submission: name, ZIP, and favourite teams."""
        payload = request.get_json(silent=True) or {}

        name = str(payload.get("name") or "").strip()[:40]
        zip_code = geocode.normalize(payload.get("zip"))
        favorites = payload.get("favorites") or {}

        if not name:
            return jsonify({"ok": False, "error": "Give the board a name"}), 400
        if not geocode.valid_zip(zip_code):
            return jsonify({"ok": False, "error": "Enter a 5-digit ZIP code"}), 400

        location = None
        try:
            location = geocode.lookup(zip_code)
        except geocode.GeocodeError:
            # Not fatal. The board is still usable; weather sits out until the
            # ZIP resolves on a later refresh.
            pass

        config.set("identity.name", name)
        config.set("identity.hostname", identity.slugify(name))
        config.set("location.zip", zip_code)
        if location:
            config.set("location.latitude", location.latitude)
            config.set("location.longitude", location.longitude)
            config.set("location.city", location.city)
            config.set("location.state", location.state)
            config.set("location.resolved_zip", location.zip)

        for league_key, abbrs in favorites.items():
            if not leagues.get(league_key):
                continue
            clean = [str(a).upper().strip()[:5] for a in abbrs if str(a).strip()]
            config.set("leagues.{}.favorites".format(league_key), clean)
            if clean:
                config.set("leagues.{}.enabled".format(league_key), True)

        config.set("setup.complete", True)
        config.save()

        hostname = identity.slugify(name)
        if hostname != identity.current_hostname():
            identity.request_rename(hostname)

        store.wake()
        return jsonify({
            "ok": True,
            "mdns": "{}.local".format(hostname),
            "location": location.label if location else "",
            "reboot_required": hostname != identity.current_hostname(),
        })

    # ---------------------------------------------------------- fantasy

    def _team_list(league_id):
        """Teams in the league, so a public-league user can pick their own.

        With a private league we can infer the user's team from the SWID in
        their cookies. A public league gives us no identity at all, so they
        have to tell us, and a dropdown of real team names is the only humane
        way to ask.
        """
        try:
            state = fantasy.league_state(
                league_id, game=config.get("fantasy.game", "football"))
        except Exception:
            return []
        return [{"id": t.id, "name": t.name, "abbrev": t.abbrev,
                 "record": t.record, "mine": t.is_mine} for t in state.teams]

    def _fantasy_status():
        stored = secrets.get(fantasy.NAMESPACE) or {}
        return {
            "enabled": bool(config.get("fantasy.enabled")),
            "league_url": config.get("fantasy.league_url", ""),
            "league_id": config.get("fantasy.league_id", ""),
            "team_id": config.get("fantasy.team_id"),
            "show_matchup": bool(config.get("fantasy.show_matchup", True)),
            "show_standings": bool(config.get("fantasy.show_standings", True)),
            # Never the values themselves, only that they exist.
            "has_credentials": bool(stored.get("espn_s2") and stored.get("swid")),
            "credential_hint": secrets.redact(stored.get("swid")),
            "needs_auth": bool(getattr(store, "fantasy_needs_auth", False)),
            "error": getattr(store, "fantasy_error", ""),
            "league_name": getattr(store.get_fantasy(), "name", "")
                           if hasattr(store, "get_fantasy") and store.get_fantasy() else "",
        }

    @app.route("/api/fantasy", methods=["GET"])
    def fantasy_status():
        return jsonify(_fantasy_status())

    @app.route("/api/fantasy/check", methods=["POST"])
    def fantasy_check():
        """Work out what this league needs, without asking which kind it is."""
        payload = request.get_json(silent=True) or {}
        result = fantasy.probe(payload.get("league_url") or "",
                               game=config.get("fantasy.game", "football"))
        if result.get("ok"):
            result["teams"] = _team_list(result["league_id"])
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.route("/api/fantasy/credentials", methods=["POST"])
    def fantasy_credentials():
        payload = request.get_json(silent=True) or {}
        espn_s2 = str(payload.get("espn_s2") or "").strip()
        swid = fantasy.clean_swid(payload.get("swid"))
        if not espn_s2 or not swid:
            return jsonify({"ok": False,
                            "error": "Both values are needed"}), 400

        secrets.put(fantasy.NAMESPACE, {"espn_s2": espn_s2, "swid": swid})
        store.fantasy_needs_auth = False

        league_id = (payload.get("league_url")
                     or config.get("fantasy.league_id") or "")
        if not league_id:
            return jsonify({"ok": True, "saved": True})

        result = fantasy.probe(league_id,
                               game=config.get("fantasy.game", "football"))
        if result.get("ok"):
            result["teams"] = _team_list(result["league_id"])
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.route("/api/fantasy/credentials", methods=["DELETE"])
    def fantasy_forget():
        secrets.clear(fantasy.NAMESPACE)
        return jsonify({"ok": True})

    @app.route("/api/fantasy/save", methods=["POST"])
    def fantasy_save():
        payload = request.get_json(silent=True) or {}
        league_url = str(payload.get("league_url") or "").strip()
        league_id = fantasy.parse_league_id(league_url)
        if league_url and not league_id:
            return jsonify({"ok": False,
                            "error": "That does not look like a league link"}), 400

        config.set("fantasy.league_url", league_url)
        config.set("fantasy.league_id", league_id)
        team_id = payload.get("team_id")
        config.set("fantasy.team_id", int(team_id) if team_id else None)
        config.set("fantasy.enabled", bool(payload.get("enabled", True))
                   and bool(league_id))
        config.set("fantasy.show_matchup", bool(payload.get("show_matchup", True)))
        config.set("fantasy.show_standings",
                   bool(payload.get("show_standings", True)))
        config.save()

        # Fetch straight away so the screens appear without waiting out the
        # refresh interval.
        store._fantasy_last = 0
        store.wake()
        return jsonify({"ok": True, "league_id": league_id})

    return app
