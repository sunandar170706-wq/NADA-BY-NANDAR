from flask import Flask, render_template_string, request, jsonify, redirect, url_for
from ytmusicapi import YTMusic
import yt_dlp

app = Flask(__name__)
yt = YTMusic()

def get_high_res_thumb(url):
    if not url:
        return ''
    if 'w60' in url or 'w120' in url:
        url = url.split('=')[0] + '=w500-h500-l90-rj'
    return url

def get_direct_audio_url(video_id):
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            return info.get('url', None)
        except Exception as e:
            print("Gagal ekstrak audio:", e)
            return None

APP_TEMPLATE = """
<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NADA BY NANDAR</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; user-select: none; }
        body { background-color: #0b0f19; color: #f8fafc; font-family: 'Outfit', sans-serif; margin: 0; padding-bottom: 120px; }
        
        #splash-screen { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: #0b0f19; display: flex; flex-direction: column; align-items: center; justify-content: center; z-index: 9999; transition: opacity 0.6s ease; }
        .pixel-logo { font-size: 30px; font-weight: 800; background: linear-gradient(135deg, #38bdf8, #ec4899); -webkit-background-clip: text; -webkit-text-fill-color: transparent; text-transform: uppercase; letter-spacing: 2px; animation: pulseGlow 1.5s infinite alternate; }
        .pixel-sub { font-size: 12px; color: #94a3b8; margin-top: 10px; letter-spacing: 2px; }
        @keyframes pulseGlow { 0% { opacity: 0.4; transform: scale(0.95); } 100% { opacity: 1; transform: scale(1.05); } }

        .top-bar { display: flex; align-items: center; justify-content: space-between; padding: 18px 20px; background: linear-gradient(135deg, #0284c7 0%, #ec4899 100%); position: sticky; top: 0; z-index: 100; box-shadow: 0 4px 20px rgba(0,0,0,0.4); }
        .brand-text { font-size: 20px; font-weight: 800; color: #ffffff; letter-spacing: -0.5px; }
        .brand-sub { font-size: 11px; font-weight: 600; color: #fdf2f8; opacity: 0.9; text-transform: uppercase; letter-spacing: 1.5px; }

        .filter-pills { display: flex; gap: 10px; padding: 15px 20px; background: #0b0f19; }
        .pill { background: #1e1b4b; color: #cbd5e1; padding: 8px 18px; border-radius: 20px; font-size: 13px; font-weight: 600; border: 1px solid #312e81; cursor: pointer; transition: 0.2s; }
        .pill.active { background: linear-gradient(135deg, #0284c7, #ec4899); color: #fff; border-color: transparent; box-shadow: 0 4px 15px rgba(236, 72, 153, 0.4); }

        .search-section { padding: 0 20px 15px 20px; }
        .search-box { display: flex; align-items: center; background: #111827; border-radius: 30px; padding: 4px 6px 4px 18px; border: 1px solid #1f2937; }
        .search-box:focus-within { border-color: #ec4899; box-shadow: 0 0 10px rgba(236, 72, 153, 0.3); }
        .search-input { flex: 1; background: transparent; border: none; outline: none; color: #fff; font-size: 14px; padding: 8px 0; font-family: 'Outfit', sans-serif; }
        .search-submit { background: linear-gradient(135deg, #0284c7, #ec4899); border: none; border-radius: 20px; color: #fff; font-weight: 700; padding: 8px 18px; font-size: 13px; cursor: pointer; }

        .section-header { padding: 10px 20px; font-size: 15px; font-weight: 700; color: #f472b6; }

        .track-list { list-style: none; padding: 0 16px; margin: 0; }
        .track-item { display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; border-radius: 12px; margin-bottom: 8px; background: #111827; border: 1px solid rgba(255,255,255,0.03); transition: transform 0.1s; }
        .track-left { display: flex; align-items: center; gap: 14px; overflow: hidden; flex: 1; cursor: pointer; }
        .track-thumb { width: 52px; height: 52px; border-radius: 10px; background: #1f2937; object-fit: cover; flex-shrink: 0; box-shadow: 0 4px 10px rgba(0,0,0,0.3); }
        .track-meta { display: flex; flex-direction: column; overflow: hidden; }
        .track-title { font-size: 14px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #fff; margin-bottom: 3px; }
        .track-artist { font-size: 12px; color: #9ca3af; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

        .track-actions { display: flex; gap: 8px; align-items: center; z-index: 5; }
        .btn-play { background: #ec4899; border: none; border-radius: 50%; width: 36px; height: 36px; color: #fff; font-size: 13px; cursor: pointer; display: flex; align-items: center; justify-content: center; box-shadow: 0 4px 10px rgba(236, 72, 153, 0.4); z-index: 10; }
        .btn-add { background: #1f2937; border: 1px solid #374151; border-radius: 50%; width: 36px; height: 36px; color: #38bdf8; font-size: 16px; font-weight: bold; cursor: pointer; display: flex; align-items: center; justify-content: center; z-index: 10; }
        .btn-add.added { background: #0284c7; color: #fff; border-color: transparent; }

        /* Fullscreen Player Modal */
        #player-modal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: linear-gradient(180deg, #1e1b4b 0%, #0b0f19 80%); z-index: 999; display: none; flex-direction: column; padding: 25px; overflow-y: auto; }
        .player-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
        .close-player { background: rgba(255,255,255,0.1); border: none; color: #fff; width: 38px; height: 38px; border-radius: 50%; display: flex; align-items: center; justify-content: center; cursor: pointer; font-size: 18px; }
        
        .big-art-container { width: 260px; height: 260px; margin: 10px auto 20px auto; border-radius: 20px; overflow: hidden; box-shadow: 0 20px 50px rgba(236, 72, 153, 0.4); background: #1f2937; flex-shrink: 0; border: 1px solid rgba(255,255,255,0.1); }
        .big-art { width: 100%; height: 100%; object-fit: cover; }
        
        .song-details { text-align: center; margin-bottom: 20px; }
        .big-title { font-size: 21px; font-weight: 700; color: #fff; margin-bottom: 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .big-artist { font-size: 14px; color: #94a3b8; }

        .player-controls { display: flex; flex-direction: column; gap: 15px; margin-bottom: 25px; }
        .progress-bar-container { width: 100%; background: #374151; height: 8px; border-radius: 4px; cursor: pointer; position: relative; overflow: hidden; }
        .progress-filled { width: 0%; height: 100%; background: linear-gradient(90deg, #0284c7, #ec4899); border-radius: 4px; transition: width 0.1s linear; }
        
        .control-buttons { display: flex; justify-content: center; align-items: center; gap: 30px; }
        .ctrl-btn { background: none; border: none; color: #fff; font-size: 24px; cursor: pointer; }
        .ctrl-main { background: linear-gradient(135deg, #0284c7, #ec4899); border-radius: 50%; width: 64px; height: 64px; display: flex; align-items: center; justify-content: center; font-size: 22px; box-shadow: 0 10px 30px rgba(236, 72, 153, 0.6); color: #fff; cursor: pointer; }

        .lyrics-box { background: rgba(17, 24, 39, 0.7); border: 1px solid rgba(56, 189, 248, 0.3); border-radius: 16px; padding: 20px; text-align: center; margin-bottom: 25px; backdrop-filter: blur(10px); min-height: 120px; max-height: 200px; overflow-y: auto; box-shadow: inset 0 2px 10px rgba(0,0,0,0.5); }
        .lyrics-title { font-size: 12px; font-weight: 700; color: #38bdf8; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; }
        .lyrics-text { font-size: 14px; color: #f1f5f9; line-height: 1.6; white-space: pre-line; }

        .suggestions-title { font-size: 16px; font-weight: 700; color: #38bdf8; margin-bottom: 15px; }

        .mini-player { position: fixed; bottom: 0; left: 0; width: 100%; background: rgba(17, 24, 39, 0.98); backdrop-filter: blur(20px); border-top: 1px solid rgba(236, 72, 153, 0.2); padding: 10px 16px; display: flex; align-items: center; justify-content: space-between; z-index: 990; cursor: pointer; }
        .mini-left { display: flex; align-items: center; gap: 12px; overflow: hidden; flex: 1; }
        .mini-thumb { width: 44px; height: 44px; border-radius: 8px; object-fit: cover; flex-shrink: 0; }
        .mini-meta { display: flex; flex-direction: column; overflow: hidden; }
        .mini-title { font-size: 13px; font-weight: 600; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .mini-artist { font-size: 11px; color: #9ca3af; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .mini-play-btn { background: #ec4899; border: none; border-radius: 50%; width: 38px; height: 38px; color: #fff; display: flex; align-items: center; justify-content: center; cursor: pointer; flex-shrink: 0; box-shadow: 0 4px 12px rgba(236, 72, 153, 0.4); }
    </style>
</head>
<body>

    <div id="splash-screen">
        <div class="pixel-logo">NADA BY NANDAR</div>
        <div class="pixel-sub">LOADING EXPERIENCE...</div>
    </div>

    <div class="top-bar">
        <div>
            <div class="brand-text">NADA</div>
            <div class="brand-sub">BY NANDAR</div>
        </div>
    </div>

    <div class="filter-pills">
        <button class="pill active" id="tab-search" onclick="switchTab('search')">Semua / Hits</button>
        <button class="pill" id="tab-playlist" onclick="switchTab('playlist')">Playlist Favorit (<span id="playlist-count">0</span>)</button>
    </div>

    <div class="search-section" id="search-box-container">
        <div class="search-box">
            <input type="text" id="query-input" class="search-input" placeholder="Cari musik kesukaanmu...">
            <button type="button" class="search-submit" onclick="performSearch()">Cari</button>
        </div>
    </div>

    <div class="section-header" id="section-title">Rekomendasi Musik Hits Untukmu</div>
    
    <ul class="track-list" id="track-list">
        {% for song in results %}
        <li class="track-item" id="track-item-{{ song.videoId }}">
            <div class="track-left" onclick="selectSongAndOpen('{{ song.videoId }}', '{{ song.title | replace("'", "") }}', '{{ song.artist | replace("'", "") }}', '{{ song.thumb }}')">
                <img src="{{ song.thumb }}" class="track-thumb" alt="Cover">
                <div class="track-meta">
                    <span class="track-title">{{ song.title }}</span>
                    <span class="track-artist">{{ song.artist }}</span>
                </div>
            </div>
            <div class="track-actions">
                <button class="btn-add" id="btn-add-{{ song.videoId }}" onclick="addToPlaylist('{{ song.videoId }}', '{{ song.title | replace("'", "") }}', '{{ song.artist | replace("'", "") }}', '{{ song.thumb }}', event)">+</button>
                <button class="btn-play" id="btn-play-{{ song.videoId }}" onclick="playSongDirect('{{ song.videoId }}', '{{ song.title | replace("'", "") }}', '{{ song.artist | replace("'", "") }}', '{{ song.thumb }}', event)">▶</button>
            </div>
        </li>
        {% endfor %}
    </ul>

    <!-- Mini Player -->
    <div class="mini-player" id="mini-player" style="display: none;" onclick="openFullscreenPlayer()">
        <div class="mini-left">
            <img id="mini-thumb" src="" class="mini-thumb" alt="">
            <div class="mini-meta">
                <span class="mini-title" id="mini-title">Judul Lagu</span>
                <span class="mini-artist" id="mini-artist">Artis</span>
            </div>
        </div>
        <button class="mini-play-btn" id="mini-play-icon" onclick="togglePlay(event)">
            <svg id="mini-icon-svg" width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>
        </button>
    </div>

    <!-- Fullscreen Player Modal -->
    <div id="player-modal">
        <div class="player-header">
            <button class="close-player" onclick="closeFullscreenPlayer()" title="Kembali">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
            </button>
            <span style="font-size: 13px; font-weight: 600; letter-spacing: 1px; color: #38bdf8;">SEDANG DIPUTAR</span>
            <div style="width: 38px;"></div>
        </div>

        <div class="big-art-container">
            <img id="big-art" src="" class="big-art" alt="Cover HD">
        </div>

        <div class="song-details">
            <div class="big-title" id="big-title">-</div>
            <div class="big-artist" id="big-artist">-</div>
        </div>

        <div class="player-controls">
            <div class="progress-bar-container" id="progress-container" onclick="seekAudio(event)">
                <div class="progress-filled" id="progress-filled"></div>
            </div>
            <div class="control-buttons">
                <button class="ctrl-btn" onclick="playPrev()">⏮</button>
                <button class="ctrl-main" id="main-play-btn" onclick="togglePlay()">
                    <svg id="main-icon-svg" width="24" height="24" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>
                </button>
                <button class="ctrl-btn" onclick="playNext()">⏭</button>
            </div>
        </div>

        <div class="lyrics-box">
            <div class="lyrics-title">Lirik Lagu</div>
            <div class="lyrics-text" id="lyrics-content">Menyiapkan lirik...</div>
        </div>

        <div class="suggestions-title">Saran Lagu Lain Untukmu</div>
        <ul class="track-list" id="suggestions-list" style="padding: 0;"></ul>
    </div>

    <audio id="audio-player" onended="playNext()" ontimeupdate="updateProgress()"></audio>

    <script>
        setTimeout(() => {
            const splash = document.getElementById('splash-screen');
            if(splash) {
                splash.style.opacity = '0';
                setTimeout(() => splash.remove(), 600);
            }
        }, 1200);

        let currentTab = 'search';
        let playlist = JSON.parse(localStorage.getItem('nandar_playlist')) || [];
        let currentQueue = [];
        let currentIndex = 0;
        let currentSongData = {};
        let audioCache = {};

        const audio = document.getElementById('audio-player');

        audio.addEventListener('play', () => updateGlobalPlayState(true));
        audio.addEventListener('pause', () => updateGlobalPlayState(false));

        document.addEventListener("DOMContentLoaded", () => {
            updatePlaylistCount();
            {% for song in results %}
            currentQueue.push({
                videoId: "{{ song.videoId }}",
                title: "{{ song.title | replace("'", "") }}",
                artist: "{{ song.artist | replace("'", "") }}",
                thumb: "{{ song.thumb }}"
            });
            {% endfor %}
            updateAllAddButtons();
        });

        function updatePlaylistCount() {
            document.getElementById('playlist-count').innerText = playlist.length;
        }

        function switchTab(tab) {
            currentTab = tab;
            document.getElementById('tab-search').classList.toggle('active', tab === 'search');
            document.getElementById('tab-playlist').classList.toggle('active', tab === 'playlist');
            document.getElementById('search-box-container').style.display = tab === 'search' ? 'block' : 'none';
            
            if (tab === 'playlist') {
                renderPlaylist();
            } else {
                performSearch();
            }
        }

        function performSearch() {
            let query = document.getElementById('query-input').value;
            fetch('/api/search?query=' + encodeURIComponent(query))
                .then(res => res.json())
                .then(data => {
                    currentQueue = data;
                    renderTrackList(data, query ? "Hasil Pencarian" : "Rekomendasi Musik Hits Untukmu");
                });
        }

        function renderTrackList(data, titleText) {
            let html = '';
            data.forEach(song => {
                let isAdded = playlist.some(p => p.videoId === song.videoId);
                let isCurrent = currentSongData.videoId === song.videoId;
                let isPlaying = isCurrent && !audio.paused;
                html += `
                <li class="track-item" id="track-item-${song.videoId}">
                    <div class="track-left" onclick="selectSongAndOpen('${song.videoId}', '${song.title}', '${song.artist}', '${song.thumb}')">
                        <img src="${song.thumb}" class="track-thumb" alt="Cover">
                        <div class="track-meta">
                            <span class="track-title">${song.title}</span>
                            <span class="track-artist">${song.artist}</span>
                        </div>
                    </div>
                    <div class="track-actions">
                        <button class="btn-add ${isAdded ? 'added' : ''}" id="btn-add-${song.videoId}" onclick="addToPlaylist('${song.videoId}', '${song.title}', '${song.artist}', '${song.thumb}', event)">${isAdded ? '✓' : '+'}</button>
                        <button class="btn-play" id="btn-play-${song.videoId}" onclick="playSongDirect('${song.videoId}', '${song.title}', '${song.artist}', '${song.thumb}', event)">${isPlaying ? '❚❚' : '▶'}</button>
                    </div>
                </li>`;
            });
            document.getElementById('track-list').innerHTML = html;
            document.getElementById('section-title').innerText = titleText;
        }

        function selectSongAndOpen(videoId, title, artist, thumb) {
            currentSongData = { videoId, title, artist, thumb };
            currentIndex = currentQueue.findIndex(s => s.videoId === videoId);
            if(currentIndex === -1) {
                currentIndex = 0;
                currentQueue.unshift(currentSongData);
            }

            document.getElementById('mini-player').style.display = 'flex';
            document.getElementById('mini-thumb').src = thumb;
            document.getElementById('mini-title').innerText = title;
            document.getElementById('mini-artist').innerText = artist;

            document.getElementById('big-art').src = thumb;
            document.getElementById('big-title').innerText = title;
            document.getElementById('big-artist').innerText = artist;

            let isPlaying = audio.src && audio.src.includes(videoId) && !audio.paused;
            updateGlobalPlayState(isPlaying);

            fetch('/api/lyrics?videoId=' + videoId + '&title=' + encodeURIComponent(title) + '&artist=' + encodeURIComponent(artist))
                .then(res => res.json())
                .then(data => {
                    document.getElementById('lyrics-content').innerText = data.lyrics;
                });

            renderSuggestions();
            openFullscreenPlayer();
            preloadAudio(videoId);
        }

        function playSongDirect(videoId, title, artist, thumb, event) {
            if(event) event.stopPropagation();
            
            audio.pause();
            audio.currentTime = 0;
            audio.src = "";

            currentSongData = { videoId, title, artist, thumb };
            
            let foundIdx = currentQueue.findIndex(s => s.videoId === videoId);
            if(foundIdx !== -1) {
                currentIndex = foundIdx;
            } else {
                currentIndex = 0;
                currentQueue.unshift(currentSongData);
            }

            document.getElementById('mini-player').style.display = 'flex';
            document.getElementById('mini-thumb').src = thumb;
            document.getElementById('mini-title').innerText = title;
            document.getElementById('mini-artist').innerText = artist;

            document.getElementById('big-art').src = thumb;
            document.getElementById('big-title').innerText = title;
            document.getElementById('big-artist').innerText = artist;

            fetch('/api/lyrics?videoId=' + videoId + '&title=' + encodeURIComponent(title) + '&artist=' + encodeURIComponent(artist))
                .then(res => res.json())
                .then(data => {
                    document.getElementById('lyrics-content').innerText = data.lyrics;
                });

            if (audioCache[videoId]) {
                audio.src = audioCache[videoId];
                audio.play();
                renderSuggestions();
            } else {
                fetch('/api/stream?videoId=' + videoId)
                    .then(res => res.json())
                    .then(data => {
                        if (data.url) {
                            audioCache[videoId] = data.url;
                            audio.src = data.url;
                            audio.play();
                            renderSuggestions();
                        }
                    });
            }
            preloadNextAudio();
            updateListPlayButtons();
        }

        function preloadAudio(videoId) {
            if (!audioCache[videoId]) {
                fetch('/api/stream?videoId=' + videoId)
                    .then(res => res.json())
                    .then(data => {
                        if (data.url) {
                            audioCache[videoId] = data.url;
                        }
                    });
            }
        }

        function preloadNextAudio() {
            if (currentQueue.length > 0) {
                let nextIdx = (currentIndex + 1) % currentQueue.length;
                preloadAudio(currentQueue[nextIdx].videoId);
            }
        }

        function togglePlay(event) {
            if(event) event.stopPropagation();
            if (!audio.src) {
                if(currentSongData.videoId) {
                    playSongDirect(currentSongData.videoId, currentSongData.title, currentSongData.artist, currentSongData.thumb);
                }
                return;
            }
            if (audio.paused) {
                audio.play();
            } else {
                audio.pause();
            }
        }

        function updateGlobalPlayState(isPlaying) {
            let pauseSvg = '<path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/>';
            let playSvg = '<path d="M8 5v14l11-7z"/>';
            
            let mainIcon = document.getElementById('main-icon-svg');
            let miniIcon = document.getElementById('mini-icon-svg');
            if(mainIcon) mainIcon.innerHTML = isPlaying ? pauseSvg : playSvg;
            if(miniIcon) miniIcon.innerHTML = isPlaying ? pauseSvg : playSvg;

            updateListPlayButtons();
        }

        function updateListPlayButtons() {
            currentQueue.forEach(song => {
                let btn = document.getElementById('btn-play-' + song.videoId);
                if (btn) {
                    let isCurrent = currentSongData.videoId === song.videoId;
                    let isPlaying = isCurrent && !audio.paused;
                    btn.innerHTML = isPlaying ? '❚❚' : '▶';
                }
            });
        }

        function playNext() {
            if (currentQueue.length > 0) {
                currentIndex = (currentIndex + 1) % currentQueue.length;
                let next = currentQueue[currentIndex];
                playSongDirect(next.videoId, next.title, next.artist, next.thumb);
            }
        }

        function playPrev() {
            if (currentQueue.length > 0) {
                currentIndex = (currentIndex - 1 + currentQueue.length) % currentQueue.length;
                let prev = currentQueue[currentIndex];
                playSongDirect(prev.videoId, prev.title, prev.artist, prev.thumb);
            }
        }

        function updateProgress() {
            if(audio.duration) {
                let pct = (audio.currentTime / audio.duration) * 100;
                let filled = document.getElementById('progress-filled');
                if(filled) filled.style.width = pct + '%';
            }
        }

        function seekAudio(event) {
            let container = document.getElementById('progress-container');
            let rect = container.getBoundingClientRect();
            let clickX = event.clientX - rect.left;
            let width = rect.width;
            if(audio.duration) {
                audio.currentTime = (clickX / width) * audio.duration;
            }
        }

        function addToPlaylist(videoId, title, artist, thumb, event) {
            if(event) event.stopPropagation();
            let isAlreadyAdded = playlist.some(s => s.videoId === videoId);
            
            if (!isAlreadyAdded) {
                playlist.push({ videoId, title, artist, thumb });
            } else {
                playlist = playlist.filter(s => s.videoId !== videoId);
            }
            
            localStorage.setItem('nandar_playlist', JSON.stringify(playlist));
            updatePlaylistCount();
            updateAllAddButtons();

            if (currentTab === 'playlist') {
                renderPlaylist();
            }
        }

        function updateAllAddButtons() {
            currentQueue.forEach(song => {
                let btnAdd = document.getElementById('btn-add-' + song.videoId);
                if (btnAdd) {
                    let isAdded = playlist.some(p => p.videoId === song.videoId);
                    if (isAdded) {
                        btnAdd.classList.add('added');
                        btnAdd.innerText = '✓';
                    } else {
                        btnAdd.classList.remove('added');
                        btnAdd.innerText = '+';
                    }
                }
            });
        }

        function renderPlaylist() {
            document.getElementById('section-title').innerText = "Playlist Favorit Kamu";
            currentQueue = playlist;
            let html = '';
            if (playlist.length === 0) {
                html = '<p style="text-align: center; color: #9ca3af; margin-top: 50px; font-size: 13px;">Playlist favoritmu masih kosong.</p>';
            } else {
                playlist.forEach(song => {
                    let isAdded = true;
                    let isCurrent = currentSongData.videoId === song.videoId;
                    let isPlaying = isCurrent && !audio.paused;
                    html += `
                    <li class="track-item" id="track-item-${song.videoId}">
                        <div class="track-left" onclick="selectSongAndOpen('${song.videoId}', '${song.title}', '${song.artist}', '${song.thumb}')">
                            <img src="${song.thumb}" class="track-thumb" alt="Cover">
                            <div class="track-meta">
                                <span class="track-title">${song.title}</span>
                                <span class="track-artist">${song.artist}</span>
                            </div>
                        </div>
                        <div class="track-actions">
                            <button class="btn-add added" id="btn-add-${song.videoId}" onclick="addToPlaylist('${song.videoId}', '${song.title}', '${song.artist}', '${song.thumb}', event)">✓</button>
                            <button class="btn-play" id="btn-play-${song.videoId}" onclick="playSongDirect('${song.videoId}', '${song.title}', '${song.artist}', '${song.thumb}', event)">${isPlaying ? '❚❚' : '▶'}</button>
                        </div>
                    </li>`;
                });
            }
            document.getElementById('track-list').innerHTML = html;
        }

        function renderSuggestions() {
            let html = '';
            let filtered = currentQueue.filter(s => s.videoId !== currentSongData.videoId).slice(0, 5);
            filtered.forEach(song => {
                let isAdded = playlist.some(p => p.videoId === song.videoId);
                let isCurrent = currentSongData.videoId === song.videoId;
                let isPlaying = isCurrent && !audio.paused;
                html += `
                <li class="track-item" id="track-item-${song.videoId}">
                    <div class="track-left" onclick="selectSongAndOpen('${song.videoId}', '${song.title}', '${song.artist}', '${song.thumb}')">
                        <img src="${song.thumb}" class="track-thumb" alt="Cover">
                        <div class="track-meta">
                            <span class="track-title">${song.title}</span>
                            <span class="track-artist">${song.artist}</span>
                        </div>
                    </div>
                    <div class="track-actions">
                        <button class="btn-add ${isAdded ? 'added' : ''}" id="btn-add-${song.videoId}" onclick="addToPlaylist('${song.videoId}', '${song.title}', '${song.artist}', '${song.thumb}', event)">${isAdded ? '✓' : '+'}</button>
                        <button class="btn-play" id="btn-play-${song.videoId}" onclick="playSongDirect('${song.videoId}', '${song.title}', '${song.artist}', '${song.thumb}', event)">${isPlaying ? '❚❚' : '▶'}</button>
                    </div>
                </li>`;
            });
            document.getElementById('suggestions-list').innerHTML = html;
        }

        function openFullscreenPlayer() {
            document.getElementById('player-modal').style.display = 'flex';
        }

        function closeFullscreenPlayer() {
            document.getElementById('player-modal').style.display = 'none';
        }
    </script>
</body>
</html>
"""

@app.route('/', methods=['GET'])
def home():
    query = request.args.get('query', '')
    results = []
    try:
        search_query = query if query else "Indonesia Hits"
        search_res = yt.search(search_query, filter="songs")
        for s in search_res[:15]:
            thumbnails = s.get('thumbnails', [])
            thumb_url = get_high_res_thumb(thumbnails[0]['url']) if thumbnails else ''
            results.append({
                'videoId': s.get('videoId'),
                'title': s.get('title'),
                'artist': s.get('artists')[0]['name'] if s.get('artists') else 'Unknown',
                'thumb': thumb_url
            })
    except Exception as e:
        print("Error:", e)
    return render_template_string(APP_TEMPLATE, results=results, query=query)

@app.route('/api/search', methods=['GET'])
def api_search():
    query = request.args.get('query', '')
    try:
        search_query = query if query else "Indonesia Hits"
        search_res = yt.search(search_query, filter="songs")
        data = []
        for s in search_res[:15]:
            thumbnails = s.get('thumbnails', [])
            thumb_url = get_high_res_thumb(thumbnails[0]['url']) if thumbnails else ''
            data.append({
                'videoId': s.get('videoId'),
                'title': s.get('title'),
                'artist': s.get('artists')[0]['name'] if s.get('artists') else 'Unknown',
                'thumb': thumb_url
            })
        return jsonify(data)
    except Exception as e:
        return jsonify([])

@app.route('/api/stream', methods=['GET'])
def api_stream():
    video_id = request.args.get('videoId')
    url = get_direct_audio_url(video_id)
    return jsonify({'url': url})

@app.route('/api/lyrics', methods=['GET'])
def api_lyrics():
    video_id = request.args.get('videoId')
    title = request.args.get('title', 'Lagu')
    artist = request.args.get('artist', 'Artis')
    
    lyrics_text = f"♫ Menampilkan informasi trek untuk '{title}' oleh {artist} ♫\n\n(Nikmati alunan musik terbaik pilihanmu bersama NADA BY NANDAR)"
    try:
        watch = yt.get_watch_playlist(video_id)
        lyrics_id = watch.get('lyrics')
        if lyrics_id:
            lyrics_data = yt.get_lyrics(lyrics_id)
            if lyrics_data and 'lyrics' in lyrics_data:
                lyrics_text = lyrics_data['lyrics']
    except Exception as e:
        print("Pencarian lirik otomatis:", e)
        
    return jsonify({'lyrics': lyrics_text})

@app.route('/play/<path:subpath>', methods=['GET'])
def catch_play(subpath):
    return redirect(url_for('home'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
