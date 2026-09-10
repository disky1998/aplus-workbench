# -*- coding: utf-8 -*-
"""商品页链接信息抓取（标题 / 7.27 小标题 / 类目 / NodeID / 五点 / 主图 / 品牌 / 价格 / 评分 / 评论）

与 A+ 抓取共用同一个已打开的页面 —— 只多跑一段 JS，不额外开浏览器、不额外加载页面。
"""
from __future__ import annotations

import re

PRODUCT_JS = r"""() => {
  try {
    const out = {
      title:'', highlights:'', category:'', nodeId:'', bullets:[], images:[],
      brand:'', price:'', rating:'', reviews:'', seller:'', availability:'',
      blocked:false, notfound:false, error:''
    };
    const txt = el => el ? (el.innerText || el.textContent || '').trim() : '';
    const one = sels => {
      for (const s of sels) {
        const el = document.querySelector(s);
        if (el) { const t = txt(el); if (t) return t; }
      }
      return '';
    };

    /* ---- 拦截 / 失效页 ---- */
    const bodyTxt = (document.body ? (document.body.innerText || '') : '').toLowerCase();
    if (bodyTxt.indexOf('robot check') > -1 ||
        bodyTxt.indexOf('type the characters you see') > -1 ||
        bodyTxt.indexOf('enter the characters you see below') > -1 ||
        document.querySelector('#captchacharacters, form[action*="validateCaptcha"], img[src*="captcha"]')) {
      out.blocked = true; return out;
    }
    if (!document.querySelector('#productTitle') &&
        (bodyTxt.indexOf("we couldn't find that page") > -1 ||
         bodyTxt.indexOf('page not found') > -1)) {
      out.notfound = true; return out;
    }

    /* ---- 标题 ---- */
    out.title = one([
      '#productTitle', '#titleSection h1', 'h1#title', 'h1.product-title-word-break',
      '#booksTitle #productTitle', '#booksTitle', 'div[data-feature-name="title"] h1',
      '#mobileProductTitle_feature_div h1', '#centerCol #title', '.product-title-word-break',
      'span#title'
    ]);
    if (!out.title) {
      const meta = document.querySelector('meta[name="title"]') || document.querySelector('meta[property="og:title"]');
      if (meta && meta.getAttribute('content')) {
        out.title = meta.getAttribute('content').trim()
          .replace(/\s*:\s*Amazon\.[a-z.]+/i, '').replace(/\s*-\s*Amazon\.[a-z.]+/i, '');
      }
    }
    if (!out.title) {
      const lds = document.querySelectorAll('script[type="application/ld+json"]');
      for (const s of lds) {
        try {
          const d = JSON.parse(s.textContent);
          if (d && d.name) { out.title = String(d.name).trim(); break; }
          if (Array.isArray(d)) {
            const p = d.find(x => x && x['@type'] === 'Product' && x.name);
            if (p) { out.title = String(p.name).trim(); break; }
          }
        } catch (e) {}
      }
    }
    out.title = out.title.replace(/\s+/g, ' ').trim();

    /* ---- 商品亮点（亚马逊 7.27 新规 Item Highlights） ---- */
    const hlSels = [
      '#itemHighlights_feature_div', 'div[data-feature-name="itemHighlights"]',
      '#productHighlights_feature_div', 'div[data-feature-name="productHighlights"]',
      '#itemHighlights', '#productHighlights', '.item-highlights-content',
      '#feature-bullets-atf', 'div[data-feature-name="item-highlights"]'
    ];
    for (const s of hlSels) {
      if (out.highlights) break;
      const el = document.querySelector(s);
      if (!el) continue;
      const li = el.querySelectorAll('li, .a-list-item, p, span.a-size-base');
      const parts = [];
      li.forEach(n => {
        const t = txt(n);
        if (t && t.length > 2 && t.toLowerCase().indexOf('highlight') === -1) parts.push(t);
      });
      if (parts.length) { out.highlights = parts.join(', '); break; }
      const whole = txt(el).replace(/^Item\s*Highlights\s*[:：]?/i, '')
                            .replace(/^Product\s*Highlights\s*[:：]?/i, '').trim();
      if (whole.length > 5) out.highlights = whole;
    }
    if (!out.highlights) {
      const ov = document.querySelector('#productOverview_feature_div, div[data-feature-name="productOverview"]');
      if (ov) {
        const specs = [];
        ov.querySelectorAll('tr, .a-row').forEach(r => {
          const lab = txt(r.querySelector('td:first-child, .a-span3, span.a-text-bold')).replace(/[:：]$/, '');
          const val = txt(r.querySelector('td:last-child, .a-span9, span.po-break-word'));
          if (lab && val && ['Brand','Manufacturer','ASIN','Item model number'].indexOf(lab) === -1) {
            specs.push(lab + ': ' + val);
          }
        });
        if (specs.length) out.highlights = specs.slice(0, 4).join(', ');
      }
    }
    if (!out.highlights) {
      const f = document.querySelector('#productFactsDesktopExpander, div[data-feature-name="productFacts"]');
      if (f) {
        const arr = [];
        f.querySelectorAll('li, .product-facts-detail').forEach(n => { const t = txt(n); if (t) arr.push(t); });
        if (arr.length) out.highlights = arr.slice(0, 3).join(', ');
      }
    }

    /* ---- 类目路径 + 最小类目节点 ---- */
    const crumbs = document.querySelectorAll(
      '#wayfinding-breadcrumbs_feature_div li .a-link-normal, ' +
      '#wayfinding-breadcrumbs_container li .a-link-normal, .a-breadcrumb li a, #departments ul li a');
    const cr = [];
    crumbs.forEach(n => { const t = txt(n); if (t && t !== '\u203a') cr.push(t); });
    if (cr.length) out.category = cr.join(' > ');

    const bl = document.querySelectorAll(
      '#wayfinding-breadcrumbs_feature_div li a, #wayfinding-breadcrumbs_container li a, .a-breadcrumb li a');
    if (bl.length) {
      const m = (bl[bl.length - 1].getAttribute('href') || '').match(/[?&]node=(\d+)/);
      if (m) out.nodeId = m[1];
    }

    /* ---- 五点描述（记录来源，便于识别降级） ---- */
    const bSels = [
      '#feature-bullets ul li span.a-list-item',
      'div[data-feature-name="featurebullets"] li span.a-list-item',
      '#featurebullets_feature_div ul li span.a-list-item',
      '#feature-bullets ul li',
      '#productFactsDesktopExpander ul li',
      'div[data-feature-name="productFacts"] li',
      '#detailBullets_feature_div ul li span.a-list-item',
      '#detailBullets_feature_div ul li',
      '#item-details ul li'
    ];
    let raw = [], src = '';
    for (const s of bSels) {
      if (raw.length) break;
      const nodes = document.querySelectorAll(s);
      if (!nodes.length) continue;
      const arr = [];
      nodes.forEach(n => { const t = txt(n); if (t) arr.push(t); });
      if (arr.length) { raw = arr; src = s; }
    }
    if (!raw.length) {
      const pd = document.querySelector('#productDescription p, #productDescription, div[data-feature-name="productDescription"]');
      if (pd) {
        const ps = [];
        pd.querySelectorAll('p').forEach(n => { const t = txt(n); if (t.length > 10) ps.push(t); });
        if (ps.length) { raw = ps.slice(0, 5); src = '#productDescription (段落兜底)'; }
        else if (txt(pd).length > 15) { raw = [txt(pd).slice(0, 250)]; src = '#productDescription (单段兜底)'; }
      }
    }
    const noise = [/click here/i, /make sure this fits/i, /to report an issue/i, /customer service/i,
                   /p\.when/i, /enter your model number/i, /for additional information/i, /visit the .* store/i];
    out.bullets = raw
      .map(t => String(t).replace(/\s+/g, ' ').trim())
      .filter(t => t.length > 8 && !noise.some(p => p.test(t)))
      .slice(0, 5);
    out.bulletsSource = src;

    /* ---- 主图（还原原始 master 大图） ---- */
    const seen = {};
    const push = (u, arr) => { const c = clean(u); if (c && !seen[c]) { seen[c] = 1; arr.push(c); } };
    function clean(u) {
      if (!u || typeof u !== 'string') return '';
      const s = u.trim();
      const i = s.indexOf('/images/I/');
      if (i === -1) return '';
      const idm = s.slice(i + 10).match(/^[A-Za-z0-9+_\-]+/);
      if (!idm) return '';
      const id = idm[0];
      if (id.indexOf('transparent-pixel') > -1 || id.indexOf('blank') > -1) return '';
      const em = s.match(/\.(jpg|jpeg|png|gif|webp)(?:\?|$)/i);
      return s.slice(0, i) + '/images/I/' + id + (em ? '.' + em[1].toLowerCase() : '.jpg');
    }
    const imgs = [];
    document.querySelectorAll('#landingImage, #imgBlkFront, #main-image, [data-action="main-image-click"] img')
      .forEach(im => {
        if (im.dataset && im.dataset.aDynamicImage) {
          try { Object.keys(JSON.parse(im.dataset.aDynamicImage)).forEach(k => push(k, imgs)); } catch (e) {}
        }
        push((im.dataset && im.dataset.oldHires) || im.src, imgs);
      });
    document.querySelectorAll('#altImages ul li img, #imageBlockThumbs img, #imageBlock .imageThumbnail img, .imageThumb img')
      .forEach(im => push((im.dataset && im.dataset.oldHires) || im.src, imgs));
    const ogi = document.querySelector('meta[property="og:image"]');
    if (ogi) push(ogi.getAttribute('content'), imgs);

    // 兜底：从页面源码里挖 colorImages / imageGalleryData
    if (imgs.length < 3) {
      const html = document.documentElement ? document.documentElement.outerHTML : '';
      const pats = [
        new RegExp("'colorImages':\\s*\\x7B\\s*'initial':\\s*(\\[[\\s\\S]*?\\])\\s*\\x7D"),
        new RegExp('"colorImages":\\s*\\x7B\\s*"initial":\\s*(\\[[\\s\\S]*?\\])\\s*\\x7D'),
        new RegExp("'imageGalleryData':\\s*(\\[[\\s\\S]*?\\])")
      ];
      for (const re of pats) {
        const m = html.match(re);
        if (!m) continue;
        try {
          JSON.parse(m[1]).forEach(d => push(d.hiRes || d.large || d.mainUrl || d.main || d.thumbUrl, imgs));
        } catch (e) {}
      }
    }
    out.images = imgs;

    /* ---- 附加信息 ---- */
    out.brand = one(['#bylineInfo', 'tr.po-brand td.a-span9', '#brand', 'a#bylineInfo'])
      .replace(/^(Brand|Visit the)\s*[:：]\s*/i, '').replace(/\s+Store$/i, '').trim();
    out.price = one([
      '#corePriceDisplay_desktop_feature_div .a-price .a-offscreen',
      '#corePrice_feature_div .a-price .a-offscreen',
      '#priceblock_ourprice', '#priceblock_dealprice', '.a-price .a-offscreen',
      '#tp_price_block_total_price_ww .a-offscreen'
    ]).replace(/[\r\n\t]+/g, ' ').trim();

    let r = '';
    const re1 = document.querySelector('#acrPopover');
    if (re1) r = re1.getAttribute('title') || txt(re1);
    if (!r) r = one(['span[data-hook="rating-out-of-text"]', '#averageCustomerReviews .a-size-base']);
    out.rating = r.replace(/\s*out of\s*5\s*stars.*$/i, '').replace(/[^\d.]/g, '').trim();

    const rv = one(['#acrCustomerReviewText', 'span[data-hook="total-review-count"]']);
    const rm = rv.match(/([\d.,]+)/);
    out.reviews = rm ? rm[1].replace(/[.,]$/, '') : '';

    out.seller = one(['#sellerProfileTriggerId', '#merchant-info',
                      'div[data-feature-name="merchantInfo"]', '#tabular-buybox .tabular-buybox-text'])
      .replace(/\s+/g, ' ').trim().slice(0, 120);
    out.availability = one(['#availability', '#availability_feature_div', '#outOfStock'])
      .replace(/\s+/g, ' ').trim().slice(0, 120);

    return out;
  } catch (err) {
    return { error: 'JS 异常: ' + (err && err.message ? err.message : String(err)) };
  }
}"""


def empty_product() -> dict:
    return {"title": "", "highlights": "", "bulletsSource": "", "category": "", "nodeId": "",
            "bullets": [], "images": [], "brand": "", "price": "", "rating": "",
            "reviews": "", "seller": "", "availability": ""}


def extract_product(page) -> dict:
    """在已打开的商品页上抓链接信息"""
    try:
        data = page.evaluate(PRODUCT_JS)
    except Exception as e:
        return {"error": f"脚本执行失败: {e}"}
    if not isinstance(data, dict):
        return {"error": "抓取结果格式异常"}
    if data.get("error"):
        return data
    if data.get("blocked"):
        return {"blocked": True, "error": "触发亚马逊验证码（Robot Check）"}
    if data.get("notfound"):
        return {"notfound": True, "error": "商品页不存在（Page Not Found）"}
    data["title"] = re.sub(r"\s+", " ", str(data.get("title") or "")).strip()
    data["bullets"] = [b for b in (data.get("bullets") or []) if str(b).strip()][:5]
    data["images"] = [u for u in (data.get("images") or []) if u]
    return data


def missing_fields(p: dict) -> list:
    """按业务重要性判断哪些关键字段缺失"""
    miss = []
    if not p.get("title"):
        miss.append("标题")
    if not p.get("bullets"):
        miss.append("五点")
    if not p.get("images"):
        miss.append("主图")
    return miss


# 导出用列顺序（与 Excel / CSV 保持一致）
EXPORT_COLUMNS = [
    ("sku", "SKU"), ("asin", "ASIN"), ("domain", "站点"), ("url", "采集链接"),
    ("title", "标题"), ("highlights", "商品亮点(小标题)"), ("category", "类目路径"),
    ("nodeId", "最小类目节点ID"), ("brand", "品牌"), ("price", "价格"),
    ("rating", "评分"), ("reviews", "评论数"), ("seller", "卖家"),
    ("bulletsSource", "五点来源"),
]
