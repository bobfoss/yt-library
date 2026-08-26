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
      ${options.headerHtml || ''}
      <div class="title-row">
        ${options.titleHtml || ''}
        ${options.actionsHtml || ''}
        <span class="entity-card-slot entity-card-actions" data-entity-card-slot="actions"></span>
      </div>
      ${options.bodyHtml || ''}
      <span class="entity-card-slot entity-card-primary-metadata" data-entity-card-slot="primaryMetadata"></span>
      ${options.lastUpdatedHtml || ''}
      <div class="entity-card-decoration-row">
        <div class="entity-card-slot entity-card-secondary-metadata" data-entity-card-slot="secondaryMetadata"></div>
        ${options.annotationMetaHtml || ''}
      </div>
      ${options.annotationNoteHtml || ''}
      <div class="search-result-summary-slot" data-search-result-slot="summaries"></div>
      ${options.tailHtml || ''}
    `;
    article.append(body);
    return article;
  }

  window.YTLibraryCollectionCard = { create };
})();
