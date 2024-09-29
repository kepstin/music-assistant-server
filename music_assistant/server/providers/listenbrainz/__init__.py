"""The ListenBrainz plugin for Music Assistant.

This plugin is only for sending listens to ListenBrainz. A separate music provider may be added
later to support playlist and radio features.
"""

from dataclasses import dataclass

from mashumaro import DataClassDictMixin

from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigEntryType,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant.common.models.enums import EventType, ExternalID
from music_assistant.common.models.event import MassEvent
from music_assistant.common.models.media_items import Radio, Track, is_track
from music_assistant.common.models.provider import ProviderManifest
from music_assistant.server import MusicAssistant
from music_assistant.server.helpers.throttle_retry import ThrottlerManager
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
    _mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    _action: str | None = None,
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
class ListenBrainzAdditionalInfo(DataClassDictMixin):
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
    tags: [str] | None = None
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


@dataclass(kw_only=True)
class ListenBrainzTrackMetadata(DataClassDictMixin):
    """Model for a metadata about a track to send to ListenBrainz."""

    artist_name: str
    track_name: str

    # Optional fields
    release_name: str | None = None
    additional_info: ListenBrainzAdditionalInfo | None = None

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

    @classmethod
    def from_media_item(cls, media_item: Track | Radio) -> "ListenBrainzTrackMetadata":
        """Create a ListenBrainz Track Metadata from a MusicAssistant MediaItem."""
        if is_track(media_item):
            return cls.from_track(cls, media_item)
        else:
            return cls.from_radio(cls, media_item)


class ListenBrainz(PluginProvider):
    """The ListenBrainz listen recording plugin."""

    throttler = ThrottlerManager(rate_limit=1)
    user_token: str
    api_base_url: str

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.user_token = self.config.get_value(CONF_USER_TOKEN)
        self.api_base_url = self.config.get_value(CONF_API_BASE_URL)

        def queue_updated(event: MassEvent) -> None:
            self._queue_updated(event)

        self.mass.subscribe(queue_updated, EventType.QUEUE_UPDATED)

        def queue_time_updated(event: MassEvent) -> None:
            self._queue_time_updated(event)

        self.mass.subscribe(queue_time_updated, EventType.QUEUE_TIME_UPDATED)


# Listens should be submitted for tracks when the user has listened to half the track or 4 minutes
# of the track, whichever is lower. If the user hasn't listened to 4 minutes or half the track, it
#  doesn't fully count as a listen and should not be submitted.
