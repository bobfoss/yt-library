(function () {
  function escapeHtml(value) {
    return String(value || '').replace(/[&<>"']/g, character => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[character]));
  }

  function detailRowHtml(items, className = 'details') {
    const filtered = (items || []).filter(Boolean);
    return filtered.length ? `<div class="${className}">${filtered.join('')}</div>` : '';
  }

  function badgeRowsHtml(badges) {
    return detailRowHtml(
      (badges || [])
        .filter(badge => badge && badge.label)
        .map(badge => {
          const title = badge.title ? ` title="${escapeHtml(badge.title)}"` : '';
          return `<span class="badge"${title}>${escapeHtml(badge.label)}</span>`;
        }),
      'details badge-lines'
    );
  }

  function creatorHtml(path, name, url) {
    if (!name) return '';
    const avatar = path ? `<img class="channel-avatar" src="/${escapeHtml(path)}" alt="">` : '';
    const content = `${avatar}<span class="creator-name">${escapeHtml(name)}</span>`;
    const attributes = String(url || '').startsWith('#') ? '' : ' target="_blank" rel="noreferrer"';
    return url
      ? `<a class="creator-link creator-chip" href="${escapeHtml(url)}"${attributes}>${content}</a>`
      : `<span class="creator-chip">${content}</span>`;
  }

  function watchProgressPercent(video) {
    const value = Number((video || {}).watch_progress_percent || 0);
    if (!Number.isFinite(value) || value <= 0) return 0;
    return Math.max(1, Math.min(100, Math.round(value)));
  }

  function thumbnailWithProgress(path, video) {
    const wrap = document.createElement('div');
    wrap.className = 'thumb-wrap';
    const image = document.createElement('img');
    image.className = 'thumb';
    image.loading = 'lazy';
    image.alt = '';
    image.src = `/${path}`;
    wrap.append(image);
    const progress = watchProgressPercent(video);
    if (progress) {
      const bar = document.createElement('div');
      bar.className = 'watch-progress';
      bar.style.width = `${progress}%`;
      wrap.append(bar);
    }
    return wrap;
  }

  function watchedLineHtml(video) {
    const progress = watchProgressPercent(video);
    const count = Number((video || {}).watch_count || 0);
    const countText = Number.isFinite(count) && count > 0
      ? ` · ${count} ${count === 1 ? 'watch' : 'watches'}`
      : '';
    if (!progress && !countText) return '';
    const progressText = progress ? ` ${progress}%` : '';
    return `<div class="watched-line">Watched${progressText}${countText}</div>`;
  }

  function watchDates(video) {
    return (Array.isArray((video || {}).watch_dates) ? video.watch_dates : [])
      .map(value => String(value || '').slice(0, 10))
      .filter(value => /^\d{4}-\d{2}-\d{2}$/.test(value))
      .sort();
  }

  function watchSparklineHtml(video, options = {}) {
    const dates = watchDates(video);
    if (!dates.length) return '';
    const times = dates.map(date => Date.parse(`${date}T00:00:00Z`)).filter(Number.isFinite);
    if (!times.length) return '';
    const detail = Boolean(options.detail);
    const width = Number(options.width || (detail ? 260 : 86));
    const height = Number(options.height || (detail ? 18 : 14));
    const padding = Number(options.padding || (detail ? 8 : 5));
    const maxBuckets = Number(options.maxBuckets || (detail ? 36 : 18));
    const bucketScale = Number(options.bucketScale || (detail ? 2.2 : 1.4));
    const min = Math.min(...times);
    const max = Math.max(...times);
    const span = Math.max(1, max - min);
    const bucketCount = Math.min(maxBuckets, Math.max(1, Math.ceil(Math.sqrt(times.length) * bucketScale)));
    const buckets = new Map();
    for (const time of times) {
      const index = span === 1 ? 0 : Math.min(bucketCount - 1, Math.floor(((time - min) / span) * bucketCount));
      const bucket = buckets.get(index) || { count: 0, time };
      bucket.count += 1;
      bucket.time = Math.max(bucket.time, time);
      buckets.set(index, bucket);
    }
    const latestIndex = [...buckets.entries()].sort((left, right) => right[1].time - left[1].time)[0]?.[0];
    const maxCount = Math.max(...[...buckets.values()].map(bucket => bucket.count));
    const dots = [...buckets.entries()].sort((left, right) => left[0] - right[0]).map(([index, bucket]) => {
      const x = bucketCount === 1
        ? width - padding
        : padding + (index / (bucketCount - 1)) * (width - padding * 2);
      const radius = Math.min(
        detail ? 5.5 : 4.2,
        (detail ? 2.2 : 1.8) + Math.sqrt(bucket.count / maxCount) * (detail ? 3.3 : 2.4)
      );
      const latest = index === latestIndex ? ' latest' : '';
      return `<circle class="spark-dot${latest}" cx="${x.toFixed(1)}" cy="${(height / 2).toFixed(1)}" r="${radius.toFixed(1)}"></circle>`;
    }).join('');
    const title = `${times.length} ${times.length === 1 ? 'watch' : 'watches'} from ${dates[0]} to ${dates[dates.length - 1]}`;
    return `<svg class="watch-sparkline${detail ? ' detail' : ''}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(title)}"><title>${escapeHtml(title)}</title>${dots}</svg>`;
  }

  function reactionLabel(video) {
    const reaction = String((video || {}).reaction || '').trim().toUpperCase();
    if (reaction === 'L') return 'Liked';
    if (reaction === 'D') return 'Disliked';
    return '';
  }

  function thumbIconHtml(kind, active) {
    const classes = `reaction-icon ${kind}${active ? ' active' : ''}`;
    return `
      <svg class="${classes}" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M7 10v11H3V10h4Zm4.8 11H19c.9 0 1.7-.6 1.9-1.5l1.1-6.2c.2-1.2-.7-2.3-1.9-2.3h-5.4l.8-3.8c.1-.6 0-1.2-.4-1.6L14.2 4 8.7 9.5c-.5.5-.7 1.1-.7 1.8V19c0 1.1.9 2 2 2h1.8Z"></path>
      </svg>
    `;
  }

  function reactionIconsHtml(video) {
    const reaction = String((video || {}).reaction || '').trim().toUpperCase();
    return `
      <div class="reaction-line" title="${escapeHtml(reactionLabel(video) || 'No like/dislike captured')}">
        ${thumbIconHtml('like', reaction === 'L')}
        ${thumbIconHtml('dislike', reaction === 'D')}
      </div>
    `;
  }

  function titleHtml(options) {
    const title = escapeHtml(options.title || '');
    if (options.titleHref) {
      const target = options.titleTarget ? ` target="${escapeHtml(options.titleTarget)}"` : '';
      const rel = options.titleTarget === '_blank' ? ' rel="noreferrer"' : '';
      return `<a class="${escapeHtml(options.titleClass || 'video-title')}" href="${escapeHtml(options.titleHref)}"${target}${rel}>${title}</a>`;
    }
    const localTitle = options.localUrl
      ? `<a class="playlist-title" href="${escapeHtml(options.localUrl)}">${title}</a>`
      : '';
    const externalTitle = options.externalUrl && options.externalIconHtml
      ? `<a class="external-link" href="${escapeHtml(options.externalUrl)}" target="_blank" rel="noreferrer" title="Open on YouTube" aria-label="Open ${title} on YouTube">${options.externalIconHtml}</a>`
      : '';
    if (localTitle || externalTitle) {
      return `<div class="title-row">${localTitle || `<div class="video-title">${title}</div>`}${externalTitle}</div>`;
    }
    return `<div class="video-title">${title}</div>`;
  }

  function create(options) {
    const article = document.createElement('article');
    article.className = ['card', options.className || ''].filter(Boolean).join(' ');
    if (options.thumbnailPath) {
      article.append(thumbnailWithProgress(options.thumbnailPath, options.progressVideo || {}));
    } else if (options.noThumbnailClass) {
      article.classList.add(options.noThumbnailClass);
    }
    const body = document.createElement('div');
    body.className = 'body';
    body.innerHTML = `
      ${options.resultKind ? `<div class="result-kind">${escapeHtml(options.resultKind)}</div>` : ''}
      ${options.position ? `<div class="position">#${escapeHtml(options.position)}</div>` : ''}
      ${titleHtml(options)}
      ${badgeRowsHtml(options.badges)}
      ${options.channelHtml ? `<div class="details">${options.channelHtml}</div>` : ''}
      ${detailRowHtml(options.details)}
      ${options.watchedHtml || ''}
      ${options.sparklineHtml || ''}
      ${options.reactionHtml || ''}
      ${options.description ? `<div class="description">${escapeHtml(options.description)}</div>` : ''}
      ${detailRowHtml(options.sources)}
      ${options.playlistSourcesHtml || ''}
    `;
    article.append(body);
    return article;
  }

  window.YTLibraryVideoCard = {
    badgeRowsHtml,
    create,
    creatorHtml,
    detailRowHtml,
    escapeHtml,
    reactionLabel,
    reactionIconsHtml,
    thumbnailWithProgress,
    watchProgressPercent,
    watchSparklineHtml,
    watchedLineHtml,
  };
})();
