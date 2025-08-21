# -*- coding: utf-8 -*-
import os
import sqlite3
import asyncio
from datetime import datetime

import wavelink
from discord.ext import commands
from discord import app_commands, Intents, Interaction
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
LAVALINK_HOST = os.getenv("LAVALINK_HOST", "127.0.0.1")
LAVALINK_PORT = int(os.getenv("LAVALINK_PORT", "2333"))
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")

intents = Intents.default()
intents.message_content = True  
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- SQLite playlist store ----------
DB_PATH = "playlists.db"

class PlaylistStore:
    def __init__(self, path: str):
        self.path = path
        self._init()

    def _init(self):
        con = sqlite3.connect(self.path)
        cur = con.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS playlists(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(guild_id, name)
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS playlist_tracks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            identifier TEXT,
            length_ms INTEGER,
            added_by INTEGER,
            added_at TEXT NOT NULL,
            FOREIGN KEY(playlist_id) REFERENCES playlists(id)
        )
        """)
        con.commit()
        con.close()

    def create_playlist(self, guild_id: int, name: str, owner_id: int):
        con = sqlite3.connect(self.path)
        cur = con.cursor()
        cur.execute("INSERT INTO playlists(guild_id,name,owner_id,created_at) VALUES(?,?,?,?)",
                    (guild_id, name, owner_id, datetime.utcnow().isoformat()))
        con.commit()
        con.close()

    def delete_playlist(self, guild_id: int, name: str) -> bool:
        con = sqlite3.connect(self.path)
        cur = con.cursor()
        cur.execute("SELECT id FROM playlists WHERE guild_id=? AND name=?", (guild_id, name))
        row = cur.fetchone()
        if not row:
            con.close()
            return False
        pid = row[0]
        cur.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (pid,))
        cur.execute("DELETE FROM playlists WHERE id=?", (pid,))
        con.commit()
        con.close()
        return True

    def get_playlist_id(self, guild_id: int, name: str):
        con = sqlite3.connect(self.path)
        cur = con.cursor()
        cur.execute("SELECT id FROM playlists WHERE guild_id=? AND name=?", (guild_id, name))
        row = cur.fetchone()
        con.close()
        return row[0] if row else None

    def add_track(self, guild_id: int, name: str, *, title: str, url: str,
                  identifier: str | None, length_ms: int | None, added_by: int):
        pid = self.get_playlist_id(guild_id, name)
        if pid is None:
            raise ValueError("Playlist not found")
        con = sqlite3.connect(self.path)
        cur = con.cursor()
        cur.execute("SELECT COALESCE(MAX(position), 0) FROM playlist_tracks WHERE playlist_id=?", (pid,))
        pos = (cur.fetchone()[0] or 0) + 1
        cur.execute("""
            INSERT INTO playlist_tracks(playlist_id,position,title,url,identifier,length_ms,added_by,added_at)
            VALUES(?,?,?,?,?,?,?,?)
        """, (pid, pos, title, url, identifier, length_ms, added_by, datetime.utcnow().isoformat()))
        con.commit()
        con.close()

    def list_tracks(self, guild_id: int, name: str):
        pid = self.get_playlist_id(guild_id, name)
        if pid is None:
            return []
        con = sqlite3.connect(self.path)
        cur = con.cursor()
        cur.execute("""
            SELECT position, title, url, identifier, length_ms FROM playlist_tracks
            WHERE playlist_id=? ORDER BY position ASC
        """, (pid,))
        rows = cur.fetchall()
        con.close()
        return rows

    def list_playlists(self, guild_id: int):
        con = sqlite3.connect(self.path)
        cur = con.cursor()
        cur.execute("SELECT name, owner_id, created_at FROM playlists WHERE guild_id=? ORDER BY name ASC", (guild_id,))
        rows = cur.fetchall()
        con.close()
        return rows

store = PlaylistStore(DB_PATH)

# --------- Simple per-guild queue ----------
class GuildQueue:
    def __init__(self):
        self.tracks: list[wavelink.Playable] = []

queues: dict[int, GuildQueue] = {}

def q_for(guild_id: int) -> GuildQueue:
    if guild_id not in queues:
        queues[guild_id] = GuildQueue()
    return queues[guild_id]

# --------- Lavalink connect ----------
async def connect_nodes():
    node = wavelink.Node(
        uri=f"http://{LAVALINK_HOST}:{LAVALINK_PORT}",
        password=LAVALINK_PASSWORD,
        secure=False,
        heartbeat=30,
    )
    await wavelink.NodePool.connect(client=bot, nodes=[node])

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        await connect_nodes()
        await bot.tree.sync()
        print("Nodes connected & slash commands synced.")
    except Exception as e:
        print("Startup error:", e)

# --------- Helpers ----------
async def ensure_voice(inter: Interaction) -> wavelink.Player | None:
    if not inter.user.voice or not inter.user.voice.channel:
        await inter.response.send_message("Join a voice channel first.", ephemeral=True)
        return None
    vc: wavelink.Player | None = inter.guild.voice_client  # type: ignore
    if vc and vc.is_connected():
        return vc
    vc = await inter.user.voice.channel.connect(cls=wavelink.Player)
    return vc

async def load_youtube(query: str) -> list[wavelink.Playable]:
    # URL vs search
    if query.startswith(("http://", "https://")):
        results = await wavelink.NodePool.get_node().get_tracks(query)
        if isinstance(results, list):
            return results
        return [results]
    # YouTube search (top 1 for add; playlist /play will handle queueing)
    tracks = await wavelink.YouTubeTrack.search(query=query, source=wavelink.TrackSource.YOUTUBE)
    return list(tracks)

# --------- Slash commands: voice & playback ----------
@bot.tree.command(description="Join your current voice channel.")
async def join(inter: Interaction):
    vc = await ensure_voice(inter)
    if vc:
        await inter.response.send_message(f"Joined **{vc.channel.name}**.")

@bot.tree.command(description="Play a YouTube URL or search.")
@app_commands.describe(query="YouTube URL or search terms")
async def play(inter: Interaction, query: str):
    vc = await ensure_voice(inter)
    if not vc:
        return
    await inter.response.defer()
    tracks = await load_youtube(query)
    if not tracks:
        await inter.followup.send("No results.")
        return

    gq = q_for(inter.guild_id)
    # If it's a playlist URL, many tracks may come back
    for t in tracks:
        gq.tracks.append(t)

    # Start immediately if idle
    if not vc.is_playing() and gq.tracks:
        await vc.play(gq.tracks.pop(0))

    if len(tracks) == 1:
        await inter.followup.send(f"Queued: **{tracks[0].title}**")
    else:
        await inter.followup.send(f"Queued **{len(tracks)}** tracks.")

@bot.tree.command(description="Show the current queue.")
async def queue(inter: Interaction):
    gq = q_for(inter.guild_id)
    if not gq.tracks:
        await inter.response.send_message("Queue is empty.")
        return
    lines = []
    for i, t in enumerate(gq.tracks[:15], start=1):
        dur = getattr(t, "length", None)
        mmss = f"{int(dur/60000)}:{int((dur%60000)/1000):02d}" if dur else "?:??"
        lines.append(f"{i}. {t.title} [{mmss}]")
    tail = "" if len(gq.tracks) <= 15 else f"\n…and {len(gq.tracks)-15} more."
    await inter.response.send_message("\n".join(lines) + tail)

@bot.tree.command(description="Skip the current track.")
async def skip(inter: Interaction):
    vc: wavelink.Player | None = inter.guild.voice_client  # type: ignore
    if not vc or not vc.is_connected():
        await inter.response.send_message("Not connected.")
        return
    await vc.stop()
    await inter.response.send_message("Skipped.")

@bot.tree.command(description="Pause playback.")
async def pause(inter: Interaction):
    vc: wavelink.Player | None = inter.guild.voice_client  # type: ignore
    if not vc or not vc.is_connected():
        await inter.response.send_message("Not connected.")
        return
    await vc.pause()
    await inter.response.send_message("Paused.")

@bot.tree.command(description="Resume playback.")
async def resume(inter: Interaction):
    vc: wavelink.Player | None = inter.guild.voice_client  # type: ignore
    if not vc or not vc.is_connected():
        await inter.response.send_message("Not connected.")
        return
    await vc.resume()
    await inter.response.send_message("Resumed.")

@bot.tree.command(description="Stop and clear the queue.")
async def stop(inter: Interaction):
    vc: wavelink.Player | None = inter.guild.voice_client  # type: ignore
    gq = q_for(inter.guild_id)
    gq.tracks.clear()
    if vc and vc.is_connected():
        await vc.stop()
    await inter.response.send_message("Stopped and cleared the queue.")

@bot.tree.command(description="Leave the voice channel.")
async def leave(inter: Interaction):
    vc: wavelink.Player | None = inter.guild.voice_client  # type: ignore
    if vc and vc.is_connected():
        await vc.disconnect()
        await inter.response.send_message("Left the channel.")
    else:
        await inter.response.send_message("I’m not in a voice channel.")

# Auto-advance
@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    vc: wavelink.Player = payload.player
    gq = q_for(vc.guild.id)
    if gq.tracks:
        await vc.play(gq.tracks.pop(0))

# --------- Playlists ----------
@bot.tree.command(description="Create a playlist for this server.")
async def playlist_create(inter: Interaction, name: str):
    try:
        store.create_playlist(inter.guild_id, name, inter.user.id)
        await inter.response.send_message(f"Created playlist **{name}**.")
    except sqlite3.IntegrityError:
        await inter.response.send_message("A playlist with that name already exists.")

@bot.tree.command(description="Delete a playlist.")
async def playlist_delete(inter: Interaction, name: str):
    ok = store.delete_playlist(inter.guild_id, name)
    if ok:
        await inter.response.send_message(f"Deleted **{name}**.")
    else:
        await inter.response.send_message("Playlist not found.")

@bot.tree.command(description="Add a track to a playlist by search or URL.")
@app_commands.describe(name="Playlist name", query="YouTube URL or search terms")
async def playlist_add(inter: Interaction, name: str, query: str):
    await inter.response.defer()
    tracks = await load_youtube(query)
    if not tracks:
        await inter.followup.send("No results.")
        return
    # take top result for search; if URL could be multiple (playlist), add them all
    added = 0
    if len(tracks) > 1 and query.startswith(("http://", "https://")):
        for t in tracks:
            store.add_track(
                inter.guild_id, name,
                title=t.title, url=t.uri, identifier=getattr(t, "identifier", None),
                length_ms=getattr(t, "length", None), added_by=inter.user.id
            )
            added += 1
    else:
        t = tracks[0]
        store.add_track(
            inter.guild_id, name,
            title=t.title, url=t.uri, identifier=getattr(t, "identifier", None),
            length_ms=getattr(t, "length", None), added_by=inter.user.id
        )
        added = 1

    await inter.followup.send(f"Added {added} track(s) to **{name}**.")

@bot.tree.command(description="Show tracks in a playlist.")
async def playlist_show(inter: Interaction, name: str):
    rows = store.list_tracks(inter.guild_id, name)
    if not rows:
        await inter.response.send_message("Playlist not found or empty.")
        return
    # rows: (position, title, url, identifier, length_ms)
    lines = []
    for pos, title, url, _, length in rows[:25]:
        if length:
            mm = length // 60000
            ss = (length % 60000) // 1000
            dur = f"{mm}:{ss:02d}"
        else:
            dur = "?:??"
        lines.append(f"{pos}. [{title}]({url}) [{dur}]")
    tail = "" if len(rows) <= 25 else f"\n…and {len(rows)-25} more."
    await inter.response.send_message("\n".join(lines) + tail, suppress_embeds=False)

@bot.tree.command(description="Play all tracks from a saved playlist.")
async def playlist_play(inter: Interaction, name: str):
    vc = await ensure_voice(inter)
    if not vc:
        return
    rows = store.list_tracks(inter.guild_id, name)
    if not rows:
        await inter.response.send_message("Playlist not found or empty.")
        return

    await inter.response.defer()
    gq = q_for(inter.guild_id)

    # Convert saved URLs back to playable tracks
    # We request them in small batches to avoid huge API bursts.
    for _, title, url, _, _ in rows:
        results = await wavelink.NodePool.get_node().get_tracks(url)
        if isinstance(results, list) and results:
            gq.tracks.append(results[0])
        elif results:
            gq.tracks.append(results)

    if not vc.is_playing() and gq.tracks:
        await vc.play(gq.tracks.pop(0))
    await inter.followup.send(f"Queued playlist **{name}** with {len(rows)} item(s).")

@bot.tree.command(description="Save your current queue into a playlist (overwrites if it exists).")
async def playlist_savequeue(inter: Interaction, name: str):
    # Ensure playlist exists (create or wipe+create)
    pid = store.get_playlist_id(inter.guild_id, name)
    if pid is not None:
        store.delete_playlist(inter.guild_id, name)
    store.create_playlist(inter.guild_id, name, inter.user.id)

    gq = q_for(inter.guild_id)
    total = 0
    for t in gq.tracks:
        store.add_track(
            inter.guild_id, name,
            title=t.title, url=t.uri, identifier=getattr(t, "identifier", None),
            length_ms=getattr(t, "length", None), added_by=inter.user.id
        )
        total += 1

    await inter.response.send_message(f"Saved {total} queued track(s) to **{name}**.")

@bot.tree.command(description="List all playlists for this server.")
async def playlist_list(inter: Interaction):
    rows = store.list_playlists(inter.guild_id)
    if not rows:
        await inter.response.send_message("No playlists yet. Try `/playlist create <name>`.")
        return
    # rows: (name, owner_id, created_at)
    lines = [f"• **{name}** (by <@{owner}>, {created_at[:10]})" for (name, owner, created_at) in rows]
    await inter.response.send_message("\n".join(lines))

bot.run(DISCORD_TOKEN)

