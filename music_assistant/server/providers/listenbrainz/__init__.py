"""The ListenBrainz plugin for Music Assistant.

This plugin is only for sending listens to ListenBrainz. A separate music provider may be added
later to support playlist and radio features.
"""

import time
from dataclasses import dataclass
from typing import Any, cast

from mashumaro.config import BaseConfig
from mashumaro.mixins.orjson import DataClassORJSONMixin

from music_assistant.common.helpers.json import json_loads
from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigEntryType,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant.common.models.enums import EventType, ExternalID, PlayerState
from music_assistant.common.models.errors import ResourceTemporarilyUnavailable
from music_assistant.common.models.event import MassEvent
from music_assistant.common.models.media_items import Radio, Track, is_track
from music_assistant.common.models.player_queue import PlayerQueue
from music_assistant.common.models.provider import ProviderManifest
from music_assistant.server import MusicAssistant
from music_assistant.server.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.server.models import ProviderInstanceType
from music_assistant.server.models.plugin import PluginProvider

DOMAIN = "listenbrainz"
CONF_USER_TOKEN = "token"
CONF_PLAYING_NOW = "playing_now"
CONF_API_BASE_URL = "api_base_url"

API_BASE_URL = "https://api.listenbrainz.org"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return ListenBrainz(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    return (
        ConfigEntry(
            key=CONF_USER_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="User token",
            description="The User token can be obtained by opening the ListenBrainz settings at "
            "https://listenbrainz.org/settings/",
            value=values.get(CONF_USER_TOKEN) if values else None,
        ),
        ConfigEntry(
            key=CONF_PLAYING_NOW,
            type=ConfigEntryType.BOOLEAN,
            label="Send Playing Now notifications",
            description="Send a notification when you begin listing to a track (it will not be "
            "permanently recorded).",
            required=False,
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_API_BASE_URL,
            type=ConfigEntryType.STRING,
            label="API base url",
            description="For testing purposes, send listens to a different server instead of "
            "listenbrainz.org",
            required=False,
            default_value=API_BASE_URL,
            category="advanced",
        ),
    )


# See the "Music service names" note at https://listenbrainz.readthedocs.io/en/latest/users/json.html#payload-json-details
MUSIC_SERVICE_DOMAIN_MAPPING = {
    "apple_music": "music.apple.com",
    "deezer": "deezer.com",
    "qobuz": "qobuz.com",
    "soundcloud": "soundcloud.com",
    "spotify": "spotify.com",
    "tidal": "tidal.com",
    "tunein": "tunein.com",
    "ytmusic": "music.youtube.com",
}


@dataclass(kw_only=True)
class ListenBrainzAdditionalInfo(DataClassORJSONMixin):
    """Model for additional identifying information about a track to send to ListenBrainz."""

    # All fields are optional, but the additional_information as a whole should be omitted if empty.
    artist_mbids: set[str] | None = None
    release_group_mbid: str | None = None
    release_mbid: str | None = None
    recording_mbid: str | None = None
    track_mbid: str | None = None
    work_mbids: set[str] | None = None
    tracknumber: int | None = None
    isrc: str | None = None
    spotify_id: str | None = None
    tags: set[str] | None = None
    # The program being used to listen to music.
    media_player: str | None = None
    media_player_version: str | None = None
    # The client that is being used to submit listens to ListenBrainz. If the media player has the
    # ability to submit listens built-in then this value may be the same as media_player.
    submission_client: str | None = None
    submission_client_version: str | None = None
    # The online source of streamed music.
    music_service: str | None = None  # Canonical domain of the online service for an online source.
    music_service_name: str | None = None
    origin_url: str | None = None  # URL to where the track is available from an online source.
    # Note: only include one of duration or duration_ms, not both.
    duration_ms: int | None = None
    duration: int | None = None

    # Non-standard fields
    discnumber: int | None = None  # "discnumber" is also used by a few other players

    class Config(BaseConfig):
        """Mashumaro serialization config."""

        omit_none = True


@dataclass(kw_only=True)
class ListenBrainzTrackMetadata(DataClassORJSONMixin):
    """Model for a metadata about a track to send to ListenBrainz."""

    artist_name: str
    track_name: str

    # Optional fields
    release_name: str | None = None
    additional_info: ListenBrainzAdditionalInfo | None = None

    class Config(BaseConfig):
        """Mashumaro serialization config."""

        omit_none = True

    @classmethod
    def from_track(cls, media_item: Track) -> "ListenBrainzTrackMetadata":
        """Create a ListenBrainz Track Metadata from a MusicAssistant Track MediaItem."""
        # This matches the logic used for creating QueueItem.name
        artist_name = "/".join(x.name for x in media_item.artists)
        track_name = media_item.name
        if media_item.version:
            track_name = f"{track_name} ({media_item.version})"

        additional_info = ListenBrainzAdditionalInfo()
        track_metadata = cls(
            artist_name=artist_name,
            track_name=track_name,
            additional_info=additional_info,
        )

        for ext_id_type, ext_id in media_item.external_ids:
            if ext_id_type == ExternalID.MB_TRACK:
                additional_info.track_mbid = ext_id
            elif ext_id_type == ExternalID.MB_RECORDING:
                additional_info.recording_mbid = ext_id
            elif ext_id_type == ExternalID.ISRC:
                additional_info.isrc = ext_id

        if media_item.duration > 0:
            additional_info.duration = media_item.duration

        artist_mbids = set()
        for artist in media_item.artists:
            for ext_id_type, ext_id in artist.external_ids:
                if ext_id_type == ExternalID.MB_ARTIST:
                    artist_mbids.add(ext_id)
        if len(artist_mbids) > 0:
            additional_info.artist_mbids = artist_mbids

        if media_item.album:
            track_metadata.release_name = media_item.album.name
            for ext_id_type, ext_id in media_item.album.external_ids:
                if ext_id_type == ExternalID.MB_ALBUM:
                    additional_info.release_mbid = ext_id
                elif ext_id_type == ExternalID.MB_RELEASEGROUP:
                    additional_info.release_group_mbid = ext_id

            additional_info.tracknumber = media_item.track_number
            additional_info.discnumber = media_item.disc_number

        for provider in media_item.provider_mappings:
            music_service = MUSIC_SERVICE_DOMAIN_MAPPING.get(provider.provider_domain)
            if music_service is None:
                continue
            additional_info.music_service = music_service
            additional_info.origin_url = provider.url
            break

        return track_metadata

    @classmethod
    def from_media_item(
        cls, media_item: Track | Radio | None
    ) -> "ListenBrainzTrackMetadata | None":
        """Create a ListenBrainz Track Metadata from a MusicAssistant MediaItem."""
        if media_item is None:
            return None

        if is_track(media_item):
            return cls.from_track(media_item)
        else:
            return None  # TODO: figure out how to handle radio


@dataclass(kw_only=True)
class ListenBrainzListenPayload(DataClassORJSONMixin):
    """Model for a ListenBrainz listen payload."""

    listened_at: int | None
    track_metadata: ListenBrainzTrackMetadata

    class Config(BaseConfig):
        """Mashumaro serialization config."""

        omit_none = True


@dataclass(kw_only=True)
class ListenBrainzPlayerQueue:
    """Player Queue state monitoring for ListenBrainz."""

    state: PlayerState = PlayerState.IDLE
    playback_started_time: float | None = None
    track_metadata: ListenBrainzTrackMetadata | None = None
    listen_recorded: bool = False


class ListenBrainz(PluginProvider):
    """The ListenBrainz listen recording plugin."""

    throttler = ThrottlerManager(rate_limit=1)
    _user_token: str
    _api_base_url: str
    _player_queues: dict[str, ListenBrainzPlayerQueue]
    _listen_queue: list[ListenBrainzListenPayload]

    def __init__(self, *args, **kwargs) -> None:
        """Initialize provider."""
        super().__init__(*args, **kwargs)
        self._player_queues = {}
        self._listen_queue = []

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._user_token = self.config.get_value(CONF_USER_TOKEN)
        self._api_base_url = self.config.get_value(CONF_API_BASE_URL)

        self.mass.subscribe(self._queue_updated, EventType.QUEUE_UPDATED)
        self.mass.subscribe(self._queue_time_updated, EventType.QUEUE_TIME_UPDATED)

    def _get_player_queue(self, queue_id: str) -> ListenBrainzPlayerQueue:
        player_queue = self._player_queues.get(queue_id)
        if player_queue is None:
            player_queue = ListenBrainzPlayerQueue()
        return player_queue

    def _create_player_queue(self, player_queue: PlayerQueue) -> ListenBrainzPlayerQueue:
        track_metadata = None
        if player_queue.state == PlayerState.PLAYING and player_queue.current_item is not None:
            self.logger.debug("Media item: %s", str(player_queue.current_item.media_item.to_dict()))
            track_metadata = ListenBrainzTrackMetadata.from_media_item(
                player_queue.current_item.media_item
            )
            if track_metadata is not None:
                track_metadata.additional_info.media_player = "Music Assistant"
                track_metadata.additional_info.media_player_version = self.mass.version
                track_metadata.additional_info.submission_client = "Music Assistant"
                track_metadata.additional_info.submission_client_version = self.mass.version
        return ListenBrainzPlayerQueue(
            state=player_queue.state,
            track_metadata=track_metadata,
        )

    def _record_listen(self, queue_id: str) -> None:
        player_queue = self._get_player_queue(queue_id)
        if player_queue.listen_recorded:
            # Already recorded this listen, nothing to do.
            return

        if player_queue.track_metadata is None or player_queue.playback_started_time is None:
            return

        # Listens should be submitted for tracks when the user has listened to half the track or
        # 4 minutes of the track, whichever is lower. If the user hasn't listened to 4 minutes or
        # half the track, it doesn't fully count as a listen and should not be submitted.
        elapsed_time = time.time() - player_queue.playback_started_time
        duration = player_queue.track_metadata.additional_info.duration
        if not (elapsed_time >= (duration * 0.5) or elapsed_time >= 4 * 60):
            return

        listen_payload = ListenBrainzListenPayload(
            listened_at=int(player_queue.playback_started_time),
            track_metadata=player_queue.track_metadata,
        )

        self.logger.debug(
            "Recording listen for %s - %s: %s",
            player_queue.track_metadata.artist_name,
            player_queue.track_metadata.track_name,
            listen_payload.to_json(),
        )

        self._listen_queue.append(listen_payload)
        self.logger.debug("Submission queue contains %i entries", len(self._listen_queue))

        # TODO: actually send the queued listens

        player_queue.listen_recorded = True

    def _notify_playing_now(self, player_queue: ListenBrainzPlayerQueue) -> None:
        if player_queue.track_metadata is None:
            return

        self.logger.debug(
            "Notifying playing now for %s - %s: %s",
            player_queue.track_metadata.artist_name,
            player_queue.track_metadata.track_name,
            player_queue.track_metadata.to_json(),
        )

    def _queue_updated(self, event: MassEvent) -> None:
        queue_id = event.object_id
        player_queue = self._get_player_queue(queue_id)
        new_player_queue = self._create_player_queue(cast(PlayerQueue, event.data))

        if new_player_queue.state != PlayerState.PLAYING:
            # Playback has been stopped or paused
            if player_queue.state == PlayerState.PLAYING:
                self._record_listen(queue_id)
        elif (
            player_queue.state == PlayerState.PLAYING
            and player_queue.track_metadata == new_player_queue.track_metadata
        ):
            # TODO: Figure out how to handle detecting restarts of looped tracks
            # No change to state affecting ListenBrainz plugin
            new_player_queue = player_queue
        else:
            # Playback has been started or playing track has changed
            new_player_queue.playback_started_time = time.time()
            self._notify_playing_now(new_player_queue)

            if player_queue.state == PlayerState.PLAYING:
                self._record_listen(queue_id)

        self._player_queues[event.object_id] = new_player_queue

    def _queue_time_updated(self, event: MassEvent) -> None:
        self._record_listen(event.object_id)

    @throttle_with_retries
    async def post_data(self, endpoint: str, **kwargs: dict[str, Any]) -> Any:
        """Get data from api."""
        url = f"http://musicbrainz.org/ws/2/{endpoint}"
        headers = {
            "User-Agent": f"Music Assistant/{self.mass.version} (https://music-assistant.io)"
        }
        kwargs["fmt"] = "json"  # type: ignore[assignment]
        async with (
            self.mass.http_session.get(url, headers=headers, params=kwargs) as response,
        ):
            # handle rate limiter
            if response.status == 429:
                backoff_time = int(response.headers.get("Retry-After", 0))
                raise ResourceTemporarilyUnavailable("Rate Limiter", backoff_time=backoff_time)
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            # handle 404 not found
            if response.status in (400, 401, 404):
                return None
            response.raise_for_status()
            return await response.json(loads=json_loads)
