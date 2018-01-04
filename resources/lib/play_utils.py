# Gnu General Public License - see LICENSE.TXT

import binascii

import xbmc
import xbmcgui
import xbmcaddon

from datetime import timedelta
import time
import json
import hashlib

from resources.lib.error import catch_except
from simple_logging import SimpleLogging
from downloadutils import DownloadUtils
from resume_dialog import ResumeDialog
from utils import PlayUtils, getArt, id_generator, send_event_notification
from kodi_utils import HomeWindow
from translation import i18n
from json_rpc import json_rpc
from datamanager import DataManager
from item_functions import get_next_episode

log = SimpleLogging(__name__)
download_utils = DownloadUtils()


@catch_except()
def playFile(play_info, monitor):

    id = play_info.get("item_id")
    auto_resume = play_info.get("auto_resume", "-1")
    force_transcode = play_info.get("force_transcode", False)
    media_source_id = play_info.get("media_source_id", "")
    use_default = play_info.get("use_default", False)

    log.debug("playFile id({0}) resume({1}) force_transcode({2})", id, auto_resume, force_transcode)

    settings = xbmcaddon.Addon('plugin.video.embycon')
    addon_path = settings.getAddonInfo('path')
    jump_back_amount = int(settings.getSetting("jump_back_amount"))

    server = download_utils.getServer()

    url = "{server}/emby/Users/{userid}/Items/" + id + "?format=json"
    data_manager = DataManager()
    result = data_manager.GetContent(url)
    log.debug("Playfile item info: {0}", result)

    if result is None:
        log.debug("Playfile item was None, so can not play!")
        return

    # select the media source to use
    media_sources = result.get('MediaSources')
    selected_media_source = None

    if media_sources is None or len(media_sources) == 0:
        log.debug("Play Failed! There is no MediaSources data!")
        return

    elif len(media_sources) == 1:
        selected_media_source = media_sources[0]

    elif media_source_id != "":
        for source in media_sources:
            if source.get("Id", "na") == media_source_id:
                selected_media_source = source
                break

    elif len(media_sources) > 1:
        sourceNames = []
        for source in media_sources:
            sourceNames.append(source.get("Name", "na"))

        dialog = xbmcgui.Dialog()
        resp = dialog.select(i18n('select_source'), sourceNames)
        if resp > -1:
            selected_media_source = media_sources[resp]
        else:
            log.debug("Play Aborted, user did not select a MediaSource")
            return

    if selected_media_source is None:
        log.debug("Play Aborted, MediaSource was None")
        return

    seekTime = 0
    auto_resume = int(auto_resume)

    # process user data for resume points
    if auto_resume != -1:
        seekTime = (auto_resume / 1000) / 10000
    else:
        userData = result.get("UserData")
        if userData.get("PlaybackPositionTicks") != 0:

            reasonableTicks = int(userData.get("PlaybackPositionTicks")) / 1000
            seekTime = reasonableTicks / 10000
            displayTime = str(timedelta(seconds=seekTime))

            resumeDialog = ResumeDialog("ResumeDialog.xml", addon_path, "default", "720p")
            resumeDialog.setResumeTime("Resume from " + displayTime)
            resumeDialog.doModal()
            resume_result = resumeDialog.getResumeAction()
            del resumeDialog
            log.debug("Resume Dialog Result: {0}", resume_result)

            # check system settings for play action
            # if prompt is set ask to set it to auto resume
            params = {"setting": "myvideos.selectaction"}
            setting_result = json_rpc('Settings.getSettingValue').execute(params)
            log.debug("Current Setting (myvideos.selectaction): {0}", setting_result)
            current_value = setting_result.get("result", None)
            if current_value is not None:
                current_value = current_value.get("value", -1)
            if current_value not in (2,3):
                return_value = xbmcgui.Dialog().yesno(i18n('extra_prompt'), i18n('turn_on_auto_resume?'))
                if return_value:
                    params = {"setting": "myvideos.selectaction", "value": 2}
                    json_rpc_result = json_rpc('Settings.setSettingValue').execute(params)
                    log.debug("Save Setting (myvideos.selectaction): {0}", json_rpc_result)

            if resume_result == 1:
                seekTime = 0
            elif resume_result == -1:
                return

    listitem_props = []
    playback_type = "0"
    playurl = None
    play_session_id = id_generator()
    log.debug("play_session_id: {0}", play_session_id)

    # check if strm file, path will contain contain strm contents
    if selected_media_source.get('Container') == 'strm':
        playurl, listitem_props = PlayUtils().getStrmDetails(selected_media_source)
        if playurl is None:
            return

    if not playurl:
        playurl, playback_type = PlayUtils().getPlayUrl(id, selected_media_source, force_transcode, play_session_id)

    log.debug("Play URL: {0} ListItem Properties: {1}", playurl, listitem_props)

    playback_type_string = "DirectPlay"
    if playback_type == "2":
        playback_type_string = "Transcode"
    elif playback_type == "1":
        playback_type_string = "DirectStream"

    # add the playback type into the overview
    if result.get("Overview", None) is not None:
        result["Overview"] = playback_type_string + "\n" + result.get("Overview")
    else:
        result["Overview"] = playback_type_string

    # add title decoration is needed
    item_title = result.get("Name", i18n('missing_title'))
    add_episode_number = settings.getSetting('addEpisodeNumber') == 'true'
    if result.get("Type") == "Episode" and add_episode_number:
        episode_num = result.get("IndexNumber")
        if episode_num is not None:
            if episode_num < 10:
                episode_num = "0" + str(episode_num)
            else:
                episode_num = str(episode_num)
        else:
            episode_num = ""
        item_title =  episode_num + " - " + item_title

    list_item = xbmcgui.ListItem(label=item_title)

    if playback_type == "2": # if transcoding then prompt for audio and subtitle
        playurl = audioSubsPref(playurl, list_item, selected_media_source, id, use_default)
        log.debug("New playurl for transcoding: {0}", playurl)

    elif playback_type == "1": # for direct stream add any streamable subtitles
        externalSubs(selected_media_source, list_item, id)

    # add playurl and data to the monitor
    data = {}
    data["item_id"] = id
    data["playback_type"] = playback_type_string
    data["play_session_id"] = play_session_id
    data["currently_playing"] = True
    monitor.played_information[playurl] = data
    log.debug("Add to played_information: {0}", monitor.played_information)

    list_item.setPath(playurl)
    list_item = setListItemProps(id, list_item, result, server, listitem_props, item_title)

    playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
    playlist.clear()
    playlist.add(playurl, list_item)
    xbmc.Player().play(playlist)

    send_next_episode_details(result)

    if seekTime == 0:
        return

    count = 0
    while not xbmc.Player().isPlaying():
        log.debug("Not playing yet...sleep for 1 sec")
        count = count + 1
        if count >= 10:
            return
        else:
            xbmc.Monitor().waitForAbort(1)

    seekTime = seekTime - jump_back_amount

    while xbmc.Player().getTime() < (seekTime - 5):
        # xbmc.Player().pause()
        xbmc.sleep(100)
        xbmc.Player().seekTime(seekTime)
        xbmc.sleep(100)
        # xbmc.Player().play()


def send_next_episode_details(item):

    next_episode = get_next_episode(item)

    if next_episode is None:
        log.debug("No next episode")
        return

    next_info = {
        "prev_id": item.get("Id"),
        "id": next_episode.get("Id"),
        "title": next_episode.get("Name")
    }

    send_event_notification("embycon_next_episode", next_info)


def setListItemProps(id, listItem, result, server, extra_props, title):
    # set up item and item info
    thumbID = id
    episode_number = -1
    season_number = -1

    art = getArt(result, server=server)
    listItem.setIconImage(art['thumb'])  # back compat
    listItem.setProperty('fanart_image', art['fanart'])  # back compat
    listItem.setProperty('discart', art['discart'])  # not avail to setArt
    listItem.setArt(art)

    listItem.setProperty('IsPlayable', 'true')
    listItem.setProperty('IsFolder', 'false')
    listItem.setProperty('id', result.get("Id"))

    for prop in extra_props:
        listItem.setProperty(prop[0], prop[1])

    item_type = result.get("Type", "").lower()
    mediatype = 'video'

    if item_type == 'movie' or item_type == 'boxset':
        mediatype = 'movie'
    elif item_type == 'series':
        mediatype = 'tvshow'
    elif item_type == 'season':
        mediatype = 'season'
    elif item_type == 'episode':
        mediatype = 'episode'

    if item_type == "episode":
        episode_number = result.get("IndexNumber", -1)

    if item_type == "episode":
        season_number = result.get("ParentIndexNumber", -1)
    elif item_type == "season":
        season_number = result.get("IndexNumber", -1)

    # play info
    details = {
        'title': title,
        'plot': result.get("Overview"),
        'mediatype': mediatype
    }

    tv_show_name = result.get("SeriesName")
    if tv_show_name is not None:
        details['tvshowtitle'] = tv_show_name

    if episode_number > -1:
        details["episode"] = str(episode_number)

    if season_number > -1:
        details["season"] = str(season_number)

    details["plotoutline"] = "emby_id:" + id
    #listItem.setUniqueIDs({'emby': id})

    listItem.setInfo("Video", infoLabels=details)

    return listItem


# For transcoding only
# Present the list of audio and subtitles to select from
# for external streamable subtitles add the URL to the Kodi item and let Kodi handle it
# else ask for the subtitles to be burnt in when transcoding
def audioSubsPref(url, list_item, media_source, item_id, use_default):

    dialog = xbmcgui.Dialog()
    audioStreamsList = {}
    audioStreams = []
    audioStreamsChannelsList = {}
    subtitleStreamsList = {}
    subtitleStreams = ['No subtitles']
    downloadableStreams = []
    selectAudioIndex = ""
    selectSubsIndex = ""
    playurlprefs = "%s" % url
    default_audio = media_source.get('DefaultAudioStreamIndex', 1)
    default_sub = media_source.get('DefaultSubtitleStreamIndex', "")

    media_streams = media_source['MediaStreams']

    for stream in media_streams:
        # Since Emby returns all possible tracks together, have to sort them.
        index = stream['Index']

        if 'Audio' in stream['Type']:
            codec = stream['Codec']
            channelLayout = stream.get('ChannelLayout', "")

            try:
                track = "%s - %s - %s %s" % (index, stream['Language'], codec, channelLayout)
            except:
                track = "%s - %s %s" % (index, codec, channelLayout)

            audioStreamsChannelsList[index] = stream['Channels']
            audioStreamsList[track] = index
            audioStreams.append(track)

        elif 'Subtitle' in stream['Type']:
            try:
                track = "%s - %s" % (index, stream['Language'])
            except:
                track = "%s - %s" % (index, stream['Codec'])

            default = stream['IsDefault']
            forced = stream['IsForced']
            downloadable = stream['IsTextSubtitleStream'] and stream['IsExternal'] and stream['SupportsExternalStream']

            if default:
                track = "%s - Default" % track
            if forced:
                track = "%s - Forced" % track
            if downloadable:
                downloadableStreams.append(index)

            subtitleStreamsList[track] = index
            subtitleStreams.append(track)

    if use_default:
        playurlprefs += "&AudioStreamIndex=%s" % default_audio

    elif len(audioStreams) > 1:
        resp = dialog.select(i18n('select_audio_stream'), audioStreams)
        if resp > -1:
            # User selected audio
            selected = audioStreams[resp]
            selectAudioIndex = audioStreamsList[selected]
            playurlprefs += "&AudioStreamIndex=%s" % selectAudioIndex
        else:  # User backed out of selection
            playurlprefs += "&AudioStreamIndex=%s" % default_audio

    else:  # There's only one audiotrack.
        selectAudioIndex = audioStreamsList[audioStreams[0]]
        playurlprefs += "&AudioStreamIndex=%s" % selectAudioIndex

    if len(subtitleStreams) > 1:
        if use_default:
            playurlprefs += "&SubtitleStreamIndex=%s" % default_sub

        else:
            resp = dialog.select(i18n('select_subtitle'), subtitleStreams)
            if resp == 0:
                # User selected no subtitles
                pass
            elif resp > -1:
                # User selected subtitles
                selected = subtitleStreams[resp]
                selectSubsIndex = subtitleStreamsList[selected]

                # Load subtitles in the listitem if downloadable
                if selectSubsIndex in downloadableStreams:
                    url = [("%s/Videos/%s/%s/Subtitles/%s/Stream.srt"
                            % (download_utils.getServer(), item_id, item_id, selectSubsIndex))]
                    log.debug("Streaming subtitles url: {0} {1}", selectSubsIndex, url)
                    list_item.setSubtitles(url)
                else:
                    # Burn subtitles
                    playurlprefs += "&SubtitleStreamIndex=%s" % selectSubsIndex

            else:  # User backed out of selection
                playurlprefs += "&SubtitleStreamIndex=%s" % default_sub

    # Get number of channels for selected audio track
    audioChannels = audioStreamsChannelsList.get(selectAudioIndex, 0)
    if audioChannels > 2:
        playurlprefs += "&AudioBitrate=384000"
    else:
        playurlprefs += "&AudioBitrate=192000"

    return playurlprefs


# direct stream, set any available subtitle streams
def externalSubs(media_source, list_item, item_id):

    externalsubs = []
    media_streams = media_source['MediaStreams']

    for stream in media_streams:

        if (stream['Type'] == "Subtitle"
                and stream['IsExternal']
                and stream['IsTextSubtitleStream']
                and stream['SupportsExternalStream']):

            index = stream['Index']
            url = ("%s/Videos/%s/%s/Subtitles/%s/Stream.%s"
                   % (download_utils.getServer(), item_id, item_id, index, stream['Codec']))

            externalsubs.append(url)

    list_item.setSubtitles(externalsubs)


def sendProgress(monitor):
    playing_file = xbmc.Player().getPlayingFile()
    play_data = monitor.played_information.get(playing_file)

    if play_data is None:
        return

    log.debug("Sending Progress Update")

    play_time = xbmc.Player().getTime()
    play_data["currentPossition"] = play_time
    play_data["currently_playing"] = True

    item_id = play_data.get("item_id")
    if item_id is None:
        return

    ticks = int(play_time * 10000000)
    paused = play_data.get("paused", False)
    playback_type = play_data.get("playback_type")
    play_session_id = play_data.get("play_session_id")

    postdata = {
        'QueueableMediaTypes': "Video",
        'CanSeek': True,
        'ItemId': item_id,
        'MediaSourceId': item_id,
        'PositionTicks': ticks,
        'IsPaused': paused,
        'IsMuted': False,
        'PlayMethod': playback_type,
        'PlaySessionId': play_session_id
    }

    log.debug("Sending POST progress started: {0}", postdata)

    url = "{server}/emby/Sessions/Playing/Progress"
    download_utils.downloadUrl(url, postBody=postdata, method="POST")


@catch_except()
def promptForStopActions(item_id, current_possition):

    settings = xbmcaddon.Addon(id='plugin.video.embycon')

    prompt_next_percentage = int(settings.getSetting('promptPlayNextEpisodePercentage'))
    play_prompt = settings.getSetting('promptPlayNextEpisodePercentage_prompt') == "true"
    prompt_delete_episode_percentage = int(settings.getSetting('promptDeleteEpisodePercentage'))
    prompt_delete_movie_percentage = int(settings.getSetting('promptDeleteMoviePercentage'))

    # everything is off so return
    if (prompt_next_percentage == 100 and
            prompt_delete_episode_percentage == 100 and
            prompt_delete_movie_percentage == 100):
        return

    jsonData = download_utils.downloadUrl("{server}/emby/Users/{userid}/Items/" + item_id + "?format=json")
    result = json.loads(jsonData)
    prompt_to_delete = False
    runtime = result.get("RunTimeTicks", 0)

    # if no runtime we cant calculate perceantge so just return
    if runtime == 0:
        log.debug("No runtime so returing")
        return

    # item percentage complete
    percenatge_complete = int(((current_possition * 10000000) / runtime) * 100)
    log.debug("Episode Percentage Complete: {0}", percenatge_complete)

    if (prompt_delete_episode_percentage < 100 and
                result.get("Type", "na") == "Episode" and
                percenatge_complete > prompt_delete_episode_percentage):
            prompt_to_delete = True

    if (prompt_delete_movie_percentage < 100 and
                result.get("Type", "na") == "Movie" and
                percenatge_complete > prompt_delete_movie_percentage):
            prompt_to_delete = True

    if prompt_to_delete:
        log.debug("Prompting for delete")
        resp = xbmcgui.Dialog().yesno(i18n('confirm_file_delete'), i18n('file_delete_confirm'), autoclose=10000)
        if resp:
            log.debug("Deleting item: {0}", item_id)
            url = "{server}/emby/Items/%s?format=json" % item_id
            download_utils.downloadUrl(url, method="DELETE")
            xbmc.executebuiltin("Container.Refresh")

    # prompt for next episode
    if (prompt_next_percentage < 100 and
                result.get("Type", "na") == "Episode" and
                percenatge_complete > prompt_next_percentage):

        next_episode = get_next_episode(result)

        if next_episode is not None:
            resp = True
            index = next_episode.get("IndexNumber", -1)
            if play_prompt:
                next_epp_name = "%02d - %s" % (index, next_episode.get("Name", "n/a"))
                resp = xbmcgui.Dialog().yesno(i18n("play_next_title"), i18n("play_next_question"), next_epp_name, autoclose=10000)

            if resp:
                next_item_id = next_episode.get("Id")
                log.debug("Playing Next Episode: {0}", next_item_id)

                play_info = {}
                play_info["item_id"] = next_item_id
                play_info["auto_resume"] = "-1"
                play_info["force_transcode"] = False
                send_event_notification("embycon_play_action", play_info)


@catch_except()
def stopAll(played_information):
    if len(played_information) == 0:
        return

    log.debug("played_information: {0}", played_information)

    for item_url in played_information:
        data = played_information.get(item_url)
        if data.get("currently_playing", False):
            log.debug("item_url: {0}", item_url)
            log.debug("item_data: {0}", data)

            current_possition = data.get("currentPossition", 0)
            emby_item_id = data.get("item_id")

            if emby_item_id is not None and len(emby_item_id) != 0 and emby_item_id != "None":
                log.debug("Playback Stopped at: {0}", current_possition)

                url = "{server}/emby/Sessions/Playing/Stopped"
                postdata = {
                    'ItemId': emby_item_id,
                    'MediaSourceId': emby_item_id,
                    'PositionTicks': int(current_possition * 10000000)
                }
                download_utils.downloadUrl(url, postBody=postdata, method="POST")
                data["currently_playing"] = False

                promptForStopActions(emby_item_id, current_possition)


class Service(xbmc.Player):

    def __init__(self, *args):
        log.debug("Starting monitor service: {0}", args)
        self.played_information = {}

    def onPlayBackStarted(self):
        # Will be called when xbmc starts playing a file
        stopAll(self.played_information)

        current_playing_file = xbmc.Player().getPlayingFile()
        log.debug("onPlayBackStarted: {0}", current_playing_file)
        log.debug("played_information: {0}", self.played_information)

        if current_playing_file not in self.played_information:
            log.debug("This file was not started by EmbyCon")
            return

        data = self.played_information[current_playing_file]
        data["paused"] = False
        data["currently_playing"] = True

        emby_item_id = data["item_id"]
        playback_type = data["playback_type"]
        play_session_id = data["play_session_id"]

        # if we could not find the ID of the current item then return
        if emby_item_id is None or len(emby_item_id) == 0:
            return

        log.debug("Sending Playback Started")
        postdata = {
            'QueueableMediaTypes': "Video",
            'CanSeek': True,
            'ItemId': emby_item_id,
            'MediaSourceId': emby_item_id,
            'PlayMethod': playback_type,
            'PlaySessionId': play_session_id
        }

        log.debug("Sending POST play started: {0}", postdata)

        url = "{server}/emby/Sessions/Playing"
        download_utils.downloadUrl(url, postBody=postdata, method="POST")

    def onPlayBackEnded(self):
        # Will be called when kodi stops playing a file
        log.debug("EmbyCon Service -> onPlayBackEnded")
        stopAll(self.played_information)

    def onPlayBackStopped(self):
        # Will be called when user stops kodi playing a file
        log.debug("onPlayBackStopped")
        stopAll(self.played_information)

    def onPlayBackPaused(self):
        # Will be called when kodi pauses the video
        log.debug("onPlayBackPaused")
        current_file = xbmc.Player().getPlayingFile()
        play_data = self.played_information.get(current_file)

        if play_data is not None:
            play_data['paused'] = True
            sendProgress(self)

    def onPlayBackResumed(self):
        # Will be called when kodi resumes the video
        log.debug("onPlayBackResumed")
        current_file = xbmc.Player().getPlayingFile()
        play_data = self.played_information.get(current_file)

        if play_data is not None:
            play_data['paused'] = False
            sendProgress(self)

    def onPlayBackSeek(self, time, seekOffset):
        # Will be called when kodi seeks in video
        log.debug("onPlayBackSeek")
        sendProgress(self)


class PlaybackService(xbmc.Monitor):

    def __init__(self, monitor):
        self.monitor = monitor

    def onNotification(self, sender, method, data):
        log.debug("PlaybackService:onNotification:{0}:{1}:{2}", sender, method, data)
        if sender[-7:] != '.SIGNAL':
            return

        signal = method.split('.', 1)[-1]
        if signal != "embycon_play_action":
            return

        data_json = json.loads(data)
        hex_data = data_json[0]
        log.debug("PlaybackService:onNotification:{0}", hex_data)
        decoded_data = binascii.unhexlify(hex_data)
        play_info = json.loads(decoded_data)
        playFile(play_info, self.monitor)
