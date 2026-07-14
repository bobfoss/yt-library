(function () {
  const escapeHtml = window.YTLibraryVideoCard.escapeHtml;

  function mediaElement(options) {
    if (!options.thumbnailPath && !options.placeholderThumbnail) return null;
    let media;
    if (options.thumbnailPath) {
      media = document.createElement('img');
      media.loading = 'lazy';
      media.alt = '';
      media.src = `/${options.thumbnailPath}`;
    } else {
      media = document.createElement('div');
      media.setAttribute('aria-hidden', 'true');
    }
    media.className = 'thumb';
    if (!options.thumbnailHref) return media;
    const link = document.createElement('a');
    link.className = 'thumb-link';
    link.href = options.thumbnailHref;
    link.append(media);
    return link;
  }

  function create(options) {
    const article = document.createElement('article');
    article.className = ['card', options.className || ''].filter(Boolean).join(' ');
    const media = mediaElement(options);
    if (media) article.append(media);
    const body = document.createElement('div');
    body.className = 'body';
    body.innerHTML = `
      ${options.resultKind ? `<div class="result-kind">${escapeHtml(options.resultKind)}</div>` : ''}
      <div class="title-row">
        ${options.titleHtml || ''}
        ${options.actionsHtml || ''}
      </div>
      ${options.bodyHtml || ''}
    `;
    article.append(body);
    return article;
  }

  window.YTLibraryCollectionCard = { create };
})();
