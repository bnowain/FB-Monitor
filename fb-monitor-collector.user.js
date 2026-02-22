// ==UserScript==
// @name         FB Monitor Collector
// @namespace    fb-monitor
// @version      1.0
// @description  Auto-expand posts/comments on Facebook, extract data, send to FB Monitor
// @match        https://www.facebook.com/*
// @match        https://m.facebook.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @connect      localhost
// @connect      127.0.0.1
// ==/UserScript==

(function () {
  'use strict';

  // --- Configuration ---
  const API_URL = GM_getValue('api_url', 'http://localhost:8000/api/ingest');
  const AUTO_EXPAND = GM_getValue('auto_expand', true);
  const EXPAND_DELAY_MS = 800;  // Delay between expand clicks

  // --- State ---
  let expanding = false;
  let expandCount = 0;
  let extractedCount = 0;

  // --- UI: Floating control panel ---
  const panel = document.createElement('div');
  panel.id = 'fbm-panel';
  panel.innerHTML = `
    <style>
      #fbm-panel {
        position: fixed; bottom: 20px; right: 20px; z-index: 99999;
        background: #1a1d27; border: 1px solid #4f8ff7; border-radius: 10px;
        padding: 12px 16px; font-family: -apple-system, sans-serif;
        color: #e1e4ed; font-size: 13px; box-shadow: 0 4px 20px rgba(0,0,0,0.5);
        min-width: 220px; user-select: none;
      }
      #fbm-panel .title { font-weight: 700; font-size: 14px; margin-bottom: 8px; color: #4f8ff7; }
      #fbm-panel .stat { color: #8b8fa3; font-size: 12px; margin: 3px 0; }
      #fbm-panel .stat b { color: #e1e4ed; }
      #fbm-panel button {
        display: block; width: 100%; margin-top: 6px; padding: 7px 0;
        border: none; border-radius: 6px; cursor: pointer;
        font-size: 13px; font-weight: 600;
      }
      #fbm-panel .btn-expand { background: #2a3a50; color: #6db3f2; }
      #fbm-panel .btn-extract { background: #4f8ff7; color: white; }
      #fbm-panel .btn-extract:hover { background: #3a6bc5; }
      #fbm-panel .btn-settings { background: none; color: #8b8fa3; font-size: 11px; margin-top: 8px; }
      #fbm-panel .btn-minimize {
        position: absolute; top: 8px; right: 10px; background: none;
        color: #8b8fa3; border: none; cursor: pointer; font-size: 16px;
        width: auto; margin: 0; padding: 0;
      }
      #fbm-panel.minimized .body { display: none; }
      #fbm-panel.minimized { min-width: auto; padding: 8px 12px; }
    </style>
    <button class="btn-minimize" id="fbm-minimize">_</button>
    <div class="title">FB Monitor</div>
    <div class="body">
      <div class="stat">Expanded: <b id="fbm-expand-count">0</b> items</div>
      <div class="stat">Auto-expand: <b id="fbm-auto-status">${AUTO_EXPAND ? 'ON' : 'OFF'}</b></div>
      <div class="stat" id="fbm-status"></div>
      <button class="btn-expand" id="fbm-expand-btn">Expand All Visible</button>
      <button class="btn-extract" id="fbm-extract-btn">Extract & Send to DB</button>
      <button class="btn-settings" id="fbm-settings-btn">Settings</button>
    </div>
  `;
  document.body.appendChild(panel);

  // Minimize toggle
  document.getElementById('fbm-minimize').addEventListener('click', () => {
    panel.classList.toggle('minimized');
  });

  // --- Expand logic ---
  const EXPAND_SELECTORS = [
    // "See more" on post text
    'div[role="button"] span:not([class])',
    // "View more comments"
    'div[role="button"]',
    // Reply expanders
    'span[role="button"]',
  ];

  const EXPAND_TEXT_PATTERNS = [
    /^see more$/i,
    /^view more comments$/i,
    /^view \d+ more comments?$/i,
    /^\d+ repl(y|ies)$/i,
    /^view \d+ more repl(y|ies)$/i,
    /^view more replies$/i,
    /^view \d+ repl(y|ies)$/i,
    /^see all$/i,
    /^view all \d+ comments$/i,
  ];

  function isExpandButton(el) {
    const text = (el.textContent || '').trim();
    if (text.length > 50) return false;
    return EXPAND_TEXT_PATTERNS.some(pattern => pattern.test(text));
  }

  function findExpandButtons() {
    const buttons = [];
    // Query for role="button" elements
    document.querySelectorAll('div[role="button"], span[role="button"]').forEach(el => {
      if (el.offsetParent !== null && isExpandButton(el)) {
        buttons.push(el);
      }
    });
    return buttons;
  }

  async function expandAllVisible() {
    const status = document.getElementById('fbm-status');
    status.textContent = 'Expanding...';

    let totalClicked = 0;
    let round = 0;
    const maxRounds = 30;

    while (round < maxRounds) {
      const buttons = findExpandButtons();
      if (buttons.length === 0) break;

      for (const btn of buttons) {
        try {
          btn.scrollIntoView({ behavior: 'smooth', block: 'center' });
          await sleep(300);
          btn.click();
          totalClicked++;
          expandCount++;
          document.getElementById('fbm-expand-count').textContent = expandCount;
          await sleep(EXPAND_DELAY_MS + Math.random() * 500);
        } catch (e) {
          // Element may have been removed after click
        }
      }
      round++;
      // Wait for new content to load
      await sleep(1500);
    }

    status.textContent = totalClicked > 0
      ? `Expanded ${totalClicked} items`
      : 'Nothing to expand';

    return totalClicked;
  }

  // --- Auto-expand with MutationObserver ---
  let autoExpandTimeout = null;

  function scheduleAutoExpand() {
    if (!AUTO_EXPAND || expanding) return;
    clearTimeout(autoExpandTimeout);
    autoExpandTimeout = setTimeout(async () => {
      const buttons = findExpandButtons();
      if (buttons.length > 0) {
        expanding = true;
        for (const btn of buttons) {
          try {
            btn.click();
            expandCount++;
            document.getElementById('fbm-expand-count').textContent = expandCount;
            await sleep(EXPAND_DELAY_MS + Math.random() * 400);
          } catch (e) {}
        }
        expanding = false;
      }
    }, 2000);
  }

  // Watch for DOM changes (new posts loading as user scrolls)
  const observer = new MutationObserver(() => {
    if (AUTO_EXPAND) scheduleAutoExpand();
  });
  observer.observe(document.body, { childList: true, subtree: true });

  // --- Extraction logic ---
  function extractPosts() {
    const posts = [];
    const articles = document.querySelectorAll('[role="article"]');

    // Track which articles are top-level posts (not comments)
    const topArticles = [];
    articles.forEach(article => {
      // Skip if this article is nested inside another article (it's a comment)
      const parent = article.parentElement?.closest('[role="article"]');
      if (!parent) {
        topArticles.push(article);
      }
    });

    for (const article of topArticles) {
      const post = extractPostFromArticle(article);
      if (post && (post.text || post.comments.length > 0)) {
        posts.push(post);
      }
    }

    return posts;
  }

  function extractPostFromArticle(article) {
    // --- Post URL and ID ---
    let postUrl = '';
    let postId = '';
    const postLinks = article.querySelectorAll('a[href*="/posts/"], a[href*="/permalink"], a[href*="story_fbid"], a[href*="/pfbid"], a[href*="/videos/"], a[href*="/reel/"], a[href*="/photo"]');
    if (postLinks.length > 0) {
      postUrl = postLinks[0].href.split('?')[0];
      // Extract post ID
      const idPatterns = [
        [/\/posts\/([\w]+)/, 1],
        [/(pfbid[\w]+)/, 1],
        [/story_fbid=(\d+)/, 1],
        [/\/videos\/(\d+)/, 1],
        [/\/reel\/(\d+)/, 1],
        [/\/permalink\/(\d+)/, 1],
        [/fbid=(\d+)/, 1],
      ];
      for (const [pattern, group] of idPatterns) {
        const match = postLinks[0].href.match(pattern);
        if (match) { postId = match[group]; break; }
      }
    }

    if (!postId) return null;

    // --- Author ---
    let author = '';
    const authorEl = article.querySelector('h2 a, h3 a, [data-ad-rendering-role="profile_name"] a, a[role="link"] > strong');
    if (authorEl) {
      author = authorEl.textContent.trim();
    }

    // --- Post text ---
    let text = '';
    const textBlocks = article.querySelectorAll('[data-ad-rendering-role="story_message"] div[dir="auto"], div[data-ad-preview="message"] div[dir="auto"]');
    if (textBlocks.length > 0) {
      text = Array.from(textBlocks).map(b => b.innerText).join('\n').trim();
    } else {
      // Fallback: find the largest text block in the article
      const allText = article.querySelectorAll('div[dir="auto"]');
      let longest = '';
      allText.forEach(el => {
        const t = el.innerText?.trim() || '';
        if (t.length > longest.length && t.length > 20) longest = t;
      });
      text = longest;
    }

    // --- Timestamp ---
    let timestamp = '';
    const tsLink = article.querySelector('a[href*="/posts/"], a[href*="/permalink"], a[href*="story_fbid"], a[href*="/pfbid"]');
    if (tsLink) {
      timestamp = tsLink.getAttribute('aria-label') || '';
      if (!timestamp) {
        // Try the text content (often "2h", "Yesterday", etc.)
        const tsSpan = tsLink.querySelector('span');
        if (tsSpan) timestamp = tsSpan.textContent.trim();
      }
    }
    if (!timestamp) {
      const abbr = article.querySelector('abbr[title], abbr[data-utime], time[datetime]');
      if (abbr) timestamp = abbr.getAttribute('title') || abbr.getAttribute('datetime') || abbr.textContent;
    }

    // --- Images (grab full-res CDN URLs) ---
    const imageUrls = [];
    const seenImageKeys = new Set();

    // First try: links wrapping images (often link to full-res photo page)
    article.querySelectorAll('a[href*="/photo"] img[src*="fbcdn"], a[href*="/photo"] img[src*="scontent"]').forEach(img => {
      let src = img.src;
      // Prefer data-src if available (may be higher res)
      if (img.dataset.src && (img.dataset.src.includes('fbcdn') || img.dataset.src.includes('scontent'))) {
        src = img.dataset.src;
      }
      if (src && !seenImageKeys.has(src)) {
        seenImageKeys.add(src);
        imageUrls.push(src);
      }
    });

    // Second pass: any fbcdn/scontent images not already captured
    article.querySelectorAll('img[src*="fbcdn"], img[src*="scontent"]').forEach(img => {
      const src = img.src;
      if (!src || seenImageKeys.has(src)) return;
      // Filter out tiny icons/avatars — only grab content images
      if (img.naturalWidth > 150 || img.width > 150 || src.includes('/p') || src.includes('_n.')) {
        seenImageKeys.add(src);
        imageUrls.push(src);
      }
    });

    // --- Videos (capture post URLs for yt-dlp, not blob: URLs) ---
    const videoUrls = [];
    // Look for video links in the post (yt-dlp works best with post/video page URLs)
    article.querySelectorAll('a[href*="/videos/"], a[href*="/watch"], a[href*="/reel/"]').forEach(a => {
      const href = a.href.split('?')[0];
      if (href && !videoUrls.includes(href)) videoUrls.push(href);
    });
    // Also capture direct video src if available (non-blob)
    article.querySelectorAll('video[src], video source[src]').forEach(v => {
      const src = v.src || v.getAttribute('src');
      if (src && !src.startsWith('blob:') && !videoUrls.includes(src)) videoUrls.push(src);
    });
    // If we see a video element but have no URLs, the post URL itself may work for yt-dlp
    if (videoUrls.length === 0 && article.querySelector('video')) {
      if (postUrl) videoUrls.push(postUrl);
    }

    // --- Reactions / counts ---
    let reactionCount = '';
    const reactionEl = article.querySelector('[aria-label*="reaction"], [aria-label*="like"]');
    if (reactionEl) reactionCount = reactionEl.getAttribute('aria-label') || '';

    let commentCountText = '';
    let shareCountText = '';
    article.querySelectorAll('span').forEach(span => {
      const t = span.textContent.trim();
      if (/^\d+\s*comments?$/i.test(t)) commentCountText = t;
      if (/^\d+\s*shares?$/i.test(t)) shareCountText = t;
    });

    // --- Shared from ---
    let sharedFrom = '';
    article.querySelectorAll('span').forEach(span => {
      const t = span.innerText || '';
      if (/shared\s+(a\s+)?(post|photo|video|link)/i.test(t)) {
        const link = span.closest('div')?.querySelector('a[role="link"]');
        if (link) sharedFrom = link.textContent.trim();
      }
    });

    // --- Links ---
    const links = [];
    article.querySelectorAll('a[href]').forEach(a => {
      let href = a.href;
      if (!href.includes('facebook.com') && !href.includes('fbcdn') && href.startsWith('http')) {
        // Handle Facebook link shim
        if (href.includes('l.facebook.com/l.php')) {
          try {
            const url = new URL(href);
            href = url.searchParams.get('u') || href;
          } catch (e) {}
        }
        if (!links.includes(href)) links.push(href);
      }
    });

    // --- Comments ---
    const comments = extractCommentsFromArticle(article);

    return {
      post_id: postId,
      post_url: postUrl,
      author,
      text,
      timestamp,
      shared_from: sharedFrom || null,
      image_urls: imageUrls,
      video_urls: videoUrls,
      reaction_count: reactionCount,
      comment_count_text: commentCountText,
      share_count_text: shareCountText,
      links,
      comments,
    };
  }

  function extractCommentsFromArticle(article) {
    const comments = [];
    const seen = new Set();

    // Find comment containers
    const commentEls = article.querySelectorAll('ul[role="list"] > li, div[aria-label*="comment" i], div[aria-label*="Comment" i]');

    commentEls.forEach(el => {
      // Author
      let author = '';
      const authorEl = el.querySelector('a[role="link"] > span > span') ||
                       el.querySelector('a[role="link"] span') ||
                       el.querySelector('a > strong') ||
                       el.querySelector('a > b');
      if (authorEl) author = authorEl.textContent.trim();

      // Text
      let text = '';
      const textEl = el.querySelector('div[dir="auto"]') || el.querySelector('span[dir="auto"]');
      if (textEl) text = textEl.innerText?.trim() || '';

      // Filter noise
      if (!text || text.length < 2) return;
      if (/^(Like|Reply|Share|Write a comment|Most relevant|Newest|All comments)$/i.test(text)) return;

      // Timestamp
      let ts = '';
      const tsEl = el.querySelector('a[href*="comment_id"] > span') || el.querySelector('abbr');
      if (tsEl) ts = tsEl.textContent?.trim() || '';

      // Is reply (nested)
      const isReply = el.closest('ul')?.closest('li') !== null;

      // Deduplicate
      const key = `${author.toLowerCase()}|${text.toLowerCase().substring(0, 100)}`;
      if (seen.has(key)) return;
      seen.add(key);

      comments.push({ author, text, timestamp: ts, is_reply: isReply });
    });

    return comments;
  }

  // --- Send to API ---
  function sendToApi(posts, pageName) {
    const status = document.getElementById('fbm-status');
    status.textContent = 'Sending to DB...';

    const payload = {
      page_name: pageName,
      page_url: window.location.href.split('?')[0],
      posts: posts,
    };

    GM_xmlhttpRequest({
      method: 'POST',
      url: API_URL,
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify(payload),
      onload: function (response) {
        try {
          const result = JSON.parse(response.responseText);
          let msg = `Saved ${result.saved || 0} posts, ${result.comments || 0} comments`;
          if (result.images_downloaded) msg += `, ${result.images_downloaded} images`;
          if (result.videos_queued) msg += `, ${result.videos_queued} videos queued`;
          status.textContent = msg;
          status.style.color = '#4caf7d';
        } catch (e) {
          status.textContent = `API responded: ${response.status}`;
          status.style.color = response.status === 200 ? '#4caf7d' : '#e05555';
        }
      },
      onerror: function (err) {
        status.textContent = 'Failed to connect to API';
        status.style.color = '#e05555';
      }
    });
  }

  // --- Button handlers ---
  document.getElementById('fbm-expand-btn').addEventListener('click', () => {
    expandAllVisible();
  });

  document.getElementById('fbm-extract-btn').addEventListener('click', () => {
    const status = document.getElementById('fbm-status');
    const posts = extractPosts();
    extractedCount = posts.length;

    if (posts.length === 0) {
      status.textContent = 'No posts found on page';
      status.style.color = '#e89b3e';
      return;
    }

    // Try to determine page name from the page header
    let pageName = '';
    const h1 = document.querySelector('h1');
    if (h1) pageName = h1.textContent.trim();

    const totalComments = posts.reduce((sum, p) => sum + p.comments.length, 0);
    const totalImages = posts.reduce((sum, p) => sum + p.image_urls.length, 0);
    const totalVideos = posts.reduce((sum, p) => sum + p.video_urls.length, 0);

    status.textContent = `Found ${posts.length} posts, ${totalComments} comments, ${totalImages} images, ${totalVideos} videos`;
    status.style.color = '#e1e4ed';

    // Also log to console for debugging
    console.log(`[FB Monitor] Extracted ${posts.length} posts:`, posts);

    // Send to API
    sendToApi(posts, pageName);
  });

  document.getElementById('fbm-settings-btn').addEventListener('click', () => {
    const newUrl = prompt('API URL:', GM_getValue('api_url', 'http://localhost:8000/api/ingest'));
    if (newUrl) {
      GM_setValue('api_url', newUrl);
      alert('API URL updated. Reload the page for it to take effect.');
    }
  });

  // --- Utilities ---
  function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  console.log('[FB Monitor] Collector loaded. Panel at bottom-right.');
})();
