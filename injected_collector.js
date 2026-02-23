/**
 * injected_collector.js — Feed-page extraction for Playwright injection.
 *
 * Stripped version of fb-monitor-collector.user.js:
 *   - Removed: Tampermonkey APIs (GM_*), UI panel, sendToApi(), MutationObserver
 *   - Kept: All extraction functions, expand logic, canvas image capture, patterns
 *   - Refactored: Split expandAllVisible() into independently callable phases
 *   - Exposed: Everything on window.__fbm for Python to call via page.evaluate()
 */
(function () {
  'use strict';

  // --- Configuration ---
  const EXPAND_DELAY_MS = 800;

  // --- Utilities ---
  function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  // --- Pattern constants ---

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
    /^view \d+ previous comments?$/i,
    /^view previous comments?$/i,
  ];

  const COMMENT_COUNT_PATTERNS = [
    /^\d+\s+comments?$/i,
  ];

  const FILTER_PATTERNS = [
    /^most relevant$/i,
    /^newest$/i,
  ];

  // --- Button finders ---

  function isExpandButton(el) {
    const text = (el.textContent || '').trim();
    if (text.length > 60) return false;
    return EXPAND_TEXT_PATTERNS.some(pattern => pattern.test(text));
  }

  function findExpandButtons() {
    const buttons = [];
    document.querySelectorAll('div[role="button"], span[role="button"]').forEach(el => {
      if (el.offsetParent !== null && isExpandButton(el)) {
        buttons.push(el);
      }
    });
    return buttons;
  }

  function findCommentCountButtons() {
    const buttons = [];
    document.querySelectorAll('div[role="button"], span[role="button"]').forEach(el => {
      if (el.offsetParent === null) return;
      const text = (el.textContent || '').trim();
      if (COMMENT_COUNT_PATTERNS.some(p => p.test(text))) {
        buttons.push(el);
      }
    });
    return buttons;
  }

  function findFilterMenus() {
    const buttons = [];
    document.querySelectorAll('div[role="button"], span[role="button"]').forEach(el => {
      if (el.offsetParent === null) return;
      const text = (el.textContent || '').trim();
      if (FILTER_PATTERNS.some(p => p.test(text))) {
        buttons.push(el);
      }
    });
    return buttons;
  }

  // =======================================================================
  // Phase 1: Open comment sections by clicking "N Comments" buttons
  // =======================================================================
  async function openCommentSections() {
    let clicked = 0;
    const commentBtns = findCommentCountButtons();
    for (const btn of commentBtns) {
      try {
        btn.scrollIntoView({ behavior: 'smooth', block: 'center' });
        await sleep(300);
        btn.click();
        clicked++;
        await sleep(EXPAND_DELAY_MS + Math.random() * 500);
      } catch (e) {}
    }
    if (commentBtns.length > 0) await sleep(2000);
    return { clicked };
  }

  // =======================================================================
  // Phase 2: Switch comment filters from "Most relevant" to "All comments"
  // =======================================================================
  async function switchToAllComments() {
    const filters = findFilterMenus();
    let switched = 0;
    for (const filterBtn of filters) {
      try {
        filterBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
        await sleep(300);
        filterBtn.click();
        await sleep(1000);

        // Look for "All comments" in the dropdown/menu
        const menuItems = document.querySelectorAll(
          'div[role="menuitem"], div[role="option"], div[role="menu"] div[role="button"]'
        );
        let found = false;
        for (const item of menuItems) {
          if (/all comments/i.test(item.textContent?.trim())) {
            item.click();
            switched++;
            found = true;
            await sleep(1500);
            break;
          }
        }

        // Fallback: try any visible element with "All comments" text
        if (!found) {
          document.querySelectorAll('span, div').forEach(el => {
            if (el.offsetParent !== null && /^all comments$/i.test(el.textContent?.trim())) {
              el.click();
              switched++;
            }
          });
          if (switched > 0) await sleep(1500);
        }
      } catch (e) {}
    }
    return { switched };
  }

  // =======================================================================
  // Phase 3: Expand threads — "See more", "View more comments", replies
  //          Called in batches from Python to avoid evaluate timeout.
  //          opts.maxRounds controls batch size (default 10).
  // =======================================================================
  async function expandThreads(opts) {
    opts = opts || {};
    const maxRounds = opts.maxRounds || 10;
    let totalClicked = 0;
    let round = 0;

    while (round < maxRounds) {
      const buttons = findExpandButtons();
      if (buttons.length === 0) break;

      for (const btn of buttons) {
        try {
          btn.scrollIntoView({ behavior: 'smooth', block: 'center' });
          await sleep(300);
          btn.click();
          totalClicked++;
          await sleep(EXPAND_DELAY_MS + Math.random() * 500);
        } catch (e) {}
      }
      round++;
      await sleep(1500);
    }

    // Check if there's more to expand
    const remaining = findExpandButtons().length;
    return { clicked: totalClicked, rounds: round, remaining };
  }

  // =======================================================================
  // Phase 4: Extract all posts + comments from the page
  // =======================================================================
  function extractPosts() {
    const posts = [];
    const articles = document.querySelectorAll('[role="article"]');

    // Track which articles are top-level posts (not nested comments)
    const topArticles = [];
    articles.forEach(article => {
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
      // Fallback: largest text block excluding comment containers
      const allText = article.querySelectorAll('div[dir="auto"]');
      let longest = '';
      allText.forEach(el => {
        if (el.closest('ul[role="list"]') || el.closest('[aria-label*="comment" i]') || el.closest('[aria-label*="Comment" i]')) return;
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
        const tsSpan = tsLink.querySelector('span');
        if (tsSpan) timestamp = tsSpan.textContent.trim();
      }
    }
    if (!timestamp) {
      const abbr = article.querySelector('abbr[title], abbr[data-utime], time[datetime]');
      if (abbr) timestamp = abbr.getAttribute('title') || abbr.getAttribute('datetime') || abbr.textContent;
    }

    // --- Images (collect DOM elements for canvas capture + URLs) ---
    const imageUrls = [];
    const imageElements = [];
    const seenImageKeys = new Set();

    article.querySelectorAll('a[href*="/photo"] img[src*="fbcdn"], a[href*="/photo"] img[src*="scontent"]').forEach(img => {
      const src = img.src;
      if (src && !seenImageKeys.has(src)) {
        seenImageKeys.add(src);
        imageUrls.push(src);
        imageElements.push(img);
      }
    });

    article.querySelectorAll('img[src*="fbcdn"], img[src*="scontent"]').forEach(img => {
      const src = img.src;
      if (!src || seenImageKeys.has(src)) return;
      if (img.naturalWidth > 150 || img.width > 150 || src.includes('/p') || src.includes('_n.')) {
        seenImageKeys.add(src);
        imageUrls.push(src);
        imageElements.push(img);
      }
    });

    // --- Videos ---
    const videoUrls = [];
    article.querySelectorAll('a[href*="/videos/"], a[href*="/watch"], a[href*="/reel/"]').forEach(a => {
      const href = a.href.split('?')[0];
      if (href && !videoUrls.includes(href)) videoUrls.push(href);
    });
    article.querySelectorAll('video[src], video source[src]').forEach(v => {
      const src = v.src || v.getAttribute('src');
      if (src && !src.startsWith('blob:') && !videoUrls.includes(src)) videoUrls.push(src);
    });
    if (videoUrls.length === 0 && article.querySelector('video')) {
      if (postUrl) videoUrls.push(postUrl);
    }

    // --- Reactions / counts ---
    let reactionCount = '';
    const reactionEl = article.querySelector('[aria-label*="reaction"], [aria-label*="like"]');
    if (reactionEl) {
      reactionCount = reactionEl.getAttribute('aria-label') || '';
      if (reactionCount && !/^\d/.test(reactionCount.trim())) reactionCount = '';
    }

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
      _image_elements: imageElements,  // DOM refs — stripped before returning to Python
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

    const commentEls = article.querySelectorAll('ul[role="list"] > li, div[aria-label*="comment" i], div[aria-label*="Comment" i]');

    commentEls.forEach(el => {
      let author = '';
      const authorEl = el.querySelector('a[role="link"] > span > span') ||
                       el.querySelector('a[role="link"] span') ||
                       el.querySelector('a > strong') ||
                       el.querySelector('a > b');
      if (authorEl) author = authorEl.textContent.trim();

      let text = '';
      const textEl = el.querySelector('div[dir="auto"]') || el.querySelector('span[dir="auto"]');
      if (textEl) text = textEl.innerText?.trim() || '';

      if (!text || text.length < 2) return;
      if (/^(Like|Reply|Share|Write a comment|Most relevant|Newest|All comments)$/i.test(text)) return;

      const noiseExact = new Set([
        'log in','forgot account?','forgot password?','sign up','create new account',
        'not now','see more','no comments yet','be the first to comment',
        'privacy','privacy policy','terms','terms of service','cookie policy',
        'cookies','ad choices','about','help','contact','careers',
        'meta','meta platforms, inc.','english (us)','english (uk)',
        'español','français','deutsch','português (brasil)','italiano',
      ]);
      if (noiseExact.has(text.toLowerCase())) return;

      const noisePatterns = [
        /^\d+[hmdws]$/i, /^\d+\s*(hr|min|sec|hour|minute|day|week)s?\s*(ago)?$/i,
        /^\d+\s+repl(y|ies)$/i, /^view\s+\d+\s+repl/i, /^most relevant/i,
        /^meta\s*[©(]/i, /^see who reacted/i, /^\d+\s*(comment|share)s?$/i,
        /^see more of/i, /^all reactions/i, /^\d+$/, /replied\s*$/i,
        /^log in or sign up/i, /^sign up to see/i, /^privacy\s*·\s*terms/i,
      ];
      if (noisePatterns.some(p => p.test(text))) return;

      let ts = '';
      const tsEl = el.querySelector('a[href*="comment_id"] > span') || el.querySelector('abbr');
      if (tsEl) ts = tsEl.textContent?.trim() || '';

      const isReply = el.closest('ul')?.closest('li') !== null;

      const key = `${author.toLowerCase()}|${text.toLowerCase().substring(0, 100)}`;
      if (seen.has(key)) return;
      seen.add(key);

      comments.push({ author, text, timestamp: ts, is_reply: isReply });
    });

    return comments;
  }

  // =======================================================================
  // Phase 5: Capture images from DOM via canvas
  // =======================================================================

  function captureImageFromCanvas(imgEl) {
    try {
      const canvas = document.createElement('canvas');
      canvas.width = imgEl.naturalWidth || imgEl.width;
      canvas.height = imgEl.naturalHeight || imgEl.height;
      if (canvas.width < 10 || canvas.height < 10) return null;

      const ctx = canvas.getContext('2d');
      ctx.drawImage(imgEl, 0, 0);
      const dataUrl = canvas.toDataURL('image/jpeg', 0.92);
      const base64 = dataUrl.split(',')[1];
      return { url: imgEl.src, data: base64, content_type: 'image/jpeg' };
    } catch (e) {
      return null;
    }
  }

  function captureImageViaCORS(imgEl) {
    return new Promise((resolve) => {
      const img = new Image();
      img.crossOrigin = 'anonymous';
      img.onload = () => {
        try {
          const canvas = document.createElement('canvas');
          canvas.width = img.naturalWidth;
          canvas.height = img.naturalHeight;
          const ctx = canvas.getContext('2d');
          ctx.drawImage(img, 0, 0);
          const dataUrl = canvas.toDataURL('image/jpeg', 0.92);
          const base64 = dataUrl.split(',')[1];
          resolve({ url: imgEl.src, data: base64, content_type: 'image/jpeg' });
        } catch (e) {
          resolve(null);
        }
      };
      img.onerror = () => resolve(null);
      img.src = imgEl.src;
      setTimeout(() => resolve(null), 5000);
    });
  }

  /**
   * captureImages(posts) — Capture base64 image data for all posts.
   *
   * This operates on the _image_elements DOM refs stored during extraction,
   * then strips those refs (they can't cross the evaluate boundary).
   * Returns the posts array with image_data populated.
   */
  async function captureImages(posts) {
    let captured = 0;
    let total = 0;

    for (const post of posts) {
      const elements = post._image_elements || [];
      total += elements.length;
      if (elements.length === 0) {
        delete post._image_elements;
        continue;
      }

      post.image_data = [];

      for (const imgEl of elements) {
        let result = captureImageFromCanvas(imgEl);
        if (!result) {
          result = await captureImageViaCORS(imgEl);
        }
        if (result) {
          post.image_data.push(result);
          captured++;
        }
      }

      // Strip DOM refs before returning to Python
      delete post._image_elements;
    }

    return { posts, captured, total };
  }

  /**
   * stripImageElements(posts) — Remove _image_elements from posts
   * (for when captureImages is skipped).
   */
  function stripImageElements(posts) {
    for (const p of posts) delete p._image_elements;
    return posts;
  }

  // --- Page name utility ---
  function getPageName() {
    const h1 = document.querySelector('h1');
    return h1 ? h1.textContent.trim() : '';
  }

  // =======================================================================
  // Expose API on window.__fbm
  // =======================================================================
  window.__fbm = {
    openCommentSections,
    switchToAllComments,
    expandThreads,
    extractPosts,
    captureImages,
    stripImageElements,
    getPageName,
  };

  console.log('[FB Monitor] Injected collector loaded. window.__fbm ready.');
})();
