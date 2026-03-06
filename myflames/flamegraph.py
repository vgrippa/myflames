"""
Generate flame graph SVG from folded stack format.
Port of flamegraph.pl core logic (flow merge + SVG output).
"""
import re
import hashlib

# Defaults (match flamegraph.pl)
XPAD = 10
FONT_SIZE = 12
FONT_WIDTH = 0.59
NAMETYPE = "Function:"
SEARCHCOLOR = "rgb(230,0,230)"
BGCOLOR1 = "#eeeeee"
BGCOLOR2 = "#eeeeb0"
BLACK = "rgb(0,0,0)"
VDGREY = "rgb(160,160,160)"
DGREY = "rgb(200,200,200)"


def _random_namehash(name):
    """Stable 'random' value from name for consistent colors."""
    h = hashlib.md5(name.encode("utf-8", errors="replace")).hexdigest()
    return int(h[:8], 16) / (16 ** 8)


def _color_hot(name):
    """Hot palette (red/orange/yellow)."""
    v1 = _random_namehash(name)
    v2 = _random_namehash(name[::-1] if name else "")
    v3 = _random_namehash(name + "x")
    r = 205 + int(50 * v3)
    g = 0 + int(230 * v1)
    b = 0 + int(55 * v2)
    return f"rgb({r},{g},{b})"


def _flow(last, this, v, node_map, tmp):
    """Merge two stacks into node_map (same algorithm as flamegraph.pl)."""
    len_a = len(last) - 1
    len_b = len(this) - 1
    len_same = 0
    for i in range(len_a + 1):
        if i > len_b or last[i] != this[i]:
            break
        len_same = i + 1
    for i in range(len_a, len_same - 1, -1):
        k = f"{last[i]};{i}"
        stime = tmp.get(k, {}).get("stime")
        delta = tmp.get(k, {}).get("delta")
        if stime is not None:
            node_id = f"{k};{v}"
            node_map[node_id] = {"stime": stime}
            if delta is not None:
                node_map[node_id]["delta"] = delta
        if k in tmp:
            del tmp[k]
    for i in range(len_same, len_b + 1):
        k = f"{this[i]};{i}"
        if k not in tmp:
            tmp[k] = {"stime": None, "delta": 0}
        tmp[k]["stime"] = v
    return this


def _parse_folded_lines(lines):
    """Parse folded format: 'frame1;frame2;frame3 count' per line. Returns (node_map, total_time)."""
    data = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(.+?)\s+(\d+(?:\.\d*)?)\s*$", line)
        if not m:
            continue
        stack, samples = m.group(1), float(m.group(2))
        data.append((stack, samples))
    data.sort(key=lambda x: x[0])
    node_map = {}
    tmp = {}
    last = [""]
    time = 0
    for stack, samples in data:
        this = [""] + [s for s in stack.split(";") if s]
        last = _flow(last, this, time, node_map, tmp)
        time += samples
    _flow(last, [], time, node_map, tmp)
    return node_map, time


def _escape_svg(s):
    if s is None:
        return ""
    s = str(s)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _strip_annotation(func):
    return re.sub(r"_\[[kwij]\]$", "", func)


def folded_to_svg(
    folded_text,
    title="Flame Graph",
    width=1200,
    height=16,
    countname="samples",
    inverted=False,
    colors="hot",
):
    """
    Convert folded stack input (string) to SVG.
    folded_text: lines of "frame1;frame2  count"
    """
    lines = folded_text.strip().splitlines() if isinstance(folded_text, str) else folded_text
    node_map, timemax = _parse_folded_lines(lines)
    if not timemax or not node_map:
        return _error_svg(width, "No valid stack counts found.")

    xpad = XPAD
    fontsize = FONT_SIZE
    fontwidth = FONT_WIDTH
    frameheight = height
    framepad = 1
    ypad1 = fontsize * 3
    ypad2 = fontsize * 2 + 10
    minwidth_f = 0.1
    widthpertime = (width - 2 * xpad) / timemax
    minwidth_time = minwidth_f / widthpertime

    # Prune and get depthmax
    depthmax = 0
    for node_id in list(node_map.keys()):
        parts = node_id.split(";")
        if len(parts) < 3:
            continue
        func, depth_str, etime_str = parts[0], parts[1], parts[2]
        try:
            depth = int(depth_str)
            etime = float(etime_str)
        except ValueError:
            continue
        stime = node_map[node_id].get("stime")
        if stime is None:
            del node_map[node_id]
            continue
        if (etime - stime) < minwidth_time:
            del node_map[node_id]
            continue
        depthmax = max(depthmax, depth)

    imageheight = (depthmax + 1) * frameheight + ypad1 + ypad2
    titlesize = fontsize + 5

    # Build JS with placeholders
    js_placeholders = {
        "xpad": xpad,
        "fontsize": fontsize,
        "fontwidth": fontwidth,
        "inverted": 1 if inverted else 0,
        "nametype": NAMETYPE,
        "searchcolor": SEARCHCOLOR,
    }

    script_content = _FLAMEGRAPH_SCRIPT % js_placeholders

    out = [
        '<?xml version="1.0" standalone="no"?>',
        '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">',
        f'<svg version="1.1" width="{width}" height="{imageheight}" onload="init(evt)" viewBox="0 0 {width} {imageheight}" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">',
        f'<defs><linearGradient id="background" y1="0" y2="1" x1="0" x2="0"><stop stop-color="{BGCOLOR1}" offset="5%"/><stop stop-color="{BGCOLOR2}" offset="95%"/></linearGradient></defs>',
        f'<style type="text/css">text {{ font-family: Verdana; font-size: {fontsize}px; fill: {BLACK}; }}',
        "#search, #ignorecase { opacity:0.1; cursor:pointer; } #search:hover, #search.show, #ignorecase:hover, #ignorecase.show { opacity:1; }",
        f"#title {{ text-anchor:middle; font-size:{titlesize}px }} #unzoom {{ cursor:pointer; }} #frames > *:hover {{ stroke:black; stroke-width:0.5; cursor:pointer; }} .hide {{ display:none; }} .parent {{ opacity:0.5; }}</style>",
        f"<script type=\"text/ecmascript\"><![CDATA[\n{script_content}\n]]></script>",
        f'<rect width="100%" height="100%" fill="url(#background)"/>',
        f'<text id="title" x="{width // 2}" y="{fontsize * 2}">{_escape_svg(title)}</text>',
        f'<text id="details" x="{xpad}" y="{imageheight - ypad2 // 2}"> </text>',
        f'<text id="unzoom" class="hide" x="{xpad}" y="{fontsize * 2}">Reset Zoom</text>',
        f'<text id="search" x="{width - xpad - 100}" y="{fontsize * 2}">Search</text>',
        f'<text id="ignorecase" x="{width - xpad - 16}" y="{fontsize * 2}">ic</text>',
        f'<text id="matched" x="{width - xpad - 100}" y="{imageheight - ypad2 // 2}"> </text>',
        '<g id="frames">',
    ]

    for node_id, node_data in sorted(node_map.items()):
        parts = node_id.split(";")
        if len(parts) < 3:
            continue
        func, depth_str, etime_str = parts[0], parts[1], parts[2]
        try:
            depth = int(depth_str)
            etime = float(etime_str)
        except ValueError:
            continue
        stime = node_data.get("stime")
        if stime is None:
            continue
        if func == "" and depth == 0:
            etime = timemax
        x1 = xpad + stime * widthpertime
        x2 = xpad + etime * widthpertime
        if inverted:
            y1 = ypad1 + depth * frameheight
            y2 = ypad1 + (depth + 1) * frameheight - framepad
        else:
            y1 = imageheight - ypad2 - (depth + 1) * frameheight + framepad
            y2 = imageheight - ypad2 - depth * frameheight
        samples = int((etime - stime) + 0.5)
        samples_txt = f"{samples:,}"
        if func == "" and depth == 0:
            info = f"all ({samples_txt} {countname}, 100%)"
        else:
            pct = f"{100 * samples / timemax:.2f}"
            esc = _escape_svg(func)
            info = f"{esc} ({samples_txt} {countname}, {pct}%)"
        if func == "--":
            color = VDGREY
        elif func == "-":
            color = DGREY
        else:
            color = _color_hot(func)
        out.append(f'<g><title>{info}</title>')
        out.append(f'<rect x="{x1:.1f}" y="{y1:.1f}" width="{x2 - x1:.1f}" height="{y2 - y1:.1f}" fill="{color}" rx="2" ry="2"/>')
        chars = int((x2 - x1) / (fontsize * fontwidth))
        text = ""
        if chars >= 3:
            text = _strip_annotation(func)[:chars]
            if len(func) > chars:
                text = text[:-2] + ".." if len(text) >= 2 else ".."
            text = _escape_svg(text)
        out.append(f'<text x="{x1 + 3:.2f}" y="{3 + (y1 + y2) / 2:.2f}">{text}</text>')
        out.append("</g>")

    out.append("</g>")
    out.append("</svg>\n")
    return "\n".join(out)


def _error_svg(width, message):
    h = 60
    return f'''<?xml version="1.0" standalone="no"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">
<svg version="1.1" width="{width}" height="{h}" xmlns="http://www.w3.org/2000/svg">
<text x="{width//2}" y="30" text-anchor="middle">{message}</text>
</svg>
'''


# Embedded JavaScript from flamegraph.pl (with placeholders: xpad, fontsize, fontwidth, inverted, nametype, searchcolor)
_FLAMEGRAPH_SCRIPT = r'''
	"use strict";
	var details, searchbtn, unzoombtn, matchedtxt, svg, searching, currentSearchTerm, ignorecase, ignorecaseBtn, pinnedDetails = null;
	function init(evt) {
		details = document.getElementById("details").firstChild;
		searchbtn = document.getElementById("search");
		ignorecaseBtn = document.getElementById("ignorecase");
		unzoombtn = document.getElementById("unzoom");
		matchedtxt = document.getElementById("matched");
		svg = document.getElementsByTagName("svg")[0];
		searching = 0;
		currentSearchTerm = null;
		var params = get_params();
		if (params.x && params.y)
			zoom(find_group(document.querySelector('[x="' + params.x + '"][y="' + params.y + '"]')));
		if (params.s) search(params.s);
	}
	window.addEventListener("click", function(e) {
		var target = find_group(e.target);
		if (target) {
			if (target.nodeName == "a") {
				if (e.ctrlKey === false) return;
				e.preventDefault();
			}
			if (target.classList.contains("parent")) { pinnedDetails = null; unzoom(true); }
			else {
			zoom(target);
			pinnedDetails = "%(nametype)s " + g_to_text(target);
			}
			if (!document.querySelector('.parent')) {
				var params = get_params();
				if (params.x) delete params.x;
				if (params.y) delete params.y;
				history.replaceState(null, null, parse_params(params));
				unzoombtn.classList.add("hide");
				return;
			}
			var el = target.querySelector("rect");
			if (el && el.attributes && el.attributes.y && el.attributes._orig_x) {
				var params = get_params();
				params.x = el.attributes._orig_x.value;
				params.y = el.attributes.y.value;
				history.replaceState(null, null, parse_params(params));
			}
		}
		else if (e.target.id == "unzoom") clearzoom();
		else if (e.target.id == "search") search_prompt();
		else if (e.target.id == "ignorecase") toggle_ignorecase();
	}, false);
	window.addEventListener("mouseover", function(e) {
		var target = find_group(e.target);
		if (target) details.nodeValue = (pinnedDetails !== null ? pinnedDetails : "%(nametype)s " + g_to_text(target));
	}, false);
	window.addEventListener("mouseout", function(e) {
		var target = find_group(e.target);
		if (target && pinnedDetails === null) details.nodeValue = ' ';
	}, false);
	window.addEventListener("keydown", function(e) {
		if (e.keyCode === 114 || (e.ctrlKey && e.keyCode === 70)) { e.preventDefault(); search_prompt(); }
		else if (e.ctrlKey && e.keyCode === 73) { e.preventDefault(); toggle_ignorecase(); }
	}, false);
	function get_params() {
		var params = {};
		var paramsarr = window.location.search.substr(1).split('&');
		for (var i = 0; i < paramsarr.length; ++i) {
			var tmp = paramsarr[i].split("=");
			if (!tmp[0] || !tmp[1]) continue;
			params[tmp[0]] = decodeURIComponent(tmp[1]);
		}
		return params;
	}
	function parse_params(params) {
		var uri = "?";
		for (var key in params) { uri += key + '=' + encodeURIComponent(params[key]) + '&'; }
		if (uri.slice(-1) == "&") uri = uri.substring(0, uri.length - 1);
		if (uri == '?') uri = window.location.href.split('?')[0];
		return uri;
	}
	function find_child(node, selector) {
		var children = node.querySelectorAll(selector);
		if (children.length) return children[0];
	}
	function find_group(node) {
		var parent = node.parentElement;
		if (!parent) return;
		if (parent.id == "frames") return node;
		return find_group(parent);
	}
	function orig_save(e, attr, val) {
		if (e.attributes["_orig_" + attr] != undefined) return;
		if (e.attributes[attr] == undefined) return;
		if (val == undefined) val = e.attributes[attr].value;
		e.setAttribute("_orig_" + attr, val);
	}
	function orig_load(e, attr) {
		if (e.attributes["_orig_"+attr] == undefined) return;
		e.attributes[attr].value = e.attributes["_orig_" + attr].value;
		e.removeAttribute("_orig_"+attr);
	}
	function g_to_text(e) {
		var text = find_child(e, "title");
		return text ? text.firstChild.nodeValue : "";
	}
	function g_to_func(e) { return g_to_text(e); }
	function update_text(e) {
		var r = find_child(e, "rect");
		var t = find_child(e, "text");
		if (!r || !t) return;
		var w = parseFloat(r.attributes.width.value) - 3;
		var txt = (find_child(e, "title") ? find_child(e, "title").textContent : "").replace(/\([^(]*\)$/,"");
		t.attributes.x.value = parseFloat(r.attributes.x.value) + 3;
		if (w < 2 * %(fontsize)s * %(fontwidth)s) { t.textContent = ""; return; }
		t.textContent = txt;
		var sl = t.getSubStringLength ? t.getSubStringLength(0, txt.length) : txt.length * 7;
		if (/^ *$/.test(txt) || sl < w) return;
		var start = Math.floor((w/sl) * txt.length);
		for (var x = start; x > 0; x = x-2) {
			if ((t.getSubStringLength ? t.getSubStringLength(0, x + 2) : (x+2)*7) <= w) {
				t.textContent = txt.substring(0, x) + "..";
				return;
			}
		}
		t.textContent = "";
	}
	function zoom_reset(e) {
		if (e.attributes != undefined) { orig_load(e, "x"); orig_load(e, "width"); }
		if (e.childNodes == undefined) return;
		for (var i = 0, c = e.childNodes; i < c.length; i++) zoom_reset(c[i]);
	}
	function zoom_child(e, x, ratio) {
		if (e.attributes != undefined) {
			if (e.attributes.x != undefined) {
				orig_save(e, "x");
				e.attributes.x.value = (parseFloat(e.attributes.x.value) - x - %(xpad)s) * ratio + %(xpad)s;
				if (e.tagName == "text" && e.parentNode) {
					var rect = find_child(e.parentNode, "rect[x]") || find_child(e.parentNode, "rect");
					if (rect) e.attributes.x.value = parseFloat(rect.attributes.x.value) + 3;
				}
			}
			if (e.attributes.width != undefined) {
				orig_save(e, "width");
				e.attributes.width.value = parseFloat(e.attributes.width.value) * ratio;
			}
		}
		if (e.childNodes == undefined) return;
		for (var i = 0, c = e.childNodes; i < c.length; i++) zoom_child(c[i], x - %(xpad)s, ratio);
	}
	function zoom_parent(e) {
		if (e.attributes) {
			if (e.attributes.x != undefined) { orig_save(e, "x"); e.attributes.x.value = %(xpad)s; }
			if (e.attributes.width != undefined) { orig_save(e, "width"); e.attributes.width.value = parseInt(svg.width.baseVal.value) - (%(xpad)s * 2); }
		}
		if (e.childNodes == undefined) return;
		for (var i = 0, c = e.childNodes; i < c.length; i++) zoom_parent(c[i]);
	}
	function zoom(node) {
		var attr = find_child(node, "rect").attributes;
		var width = parseFloat(attr.width.value);
		var xmin = parseFloat(attr.x.value);
		var xmax = parseFloat(xmin + width);
		var ymin = parseFloat(attr.y.value);
		var ratio = (svg.width.baseVal.value - 2 * %(xpad)s) / width;
		var fudge = 0.0001;
		unzoombtn.classList.remove("hide");
		var el = document.getElementById("frames").children;
		for (var i = 0; i < el.length; i++) {
			var e = el[i];
			var a = find_child(e, "rect").attributes;
			var ex = parseFloat(a.x.value);
			var ew = parseFloat(a.width.value);
			var upstack = (%(inverted)s == 0) ? (parseFloat(a.y.value) > ymin) : (parseFloat(a.y.value) < ymin);
			if (upstack) {
				if (ex <= xmin && (ex+ew+fudge) >= xmax) {
					e.classList.add("parent");
					zoom_parent(e);
					update_text(e);
				} else e.classList.add("hide");
			} else {
				if (ex < xmin || ex + fudge >= xmax) e.classList.add("hide");
				else { zoom_child(e, xmin, ratio); update_text(e); }
			}
		}
		search();
	}
	function unzoom(dont_update_text) {
		unzoombtn.classList.add("hide");
		var el = document.getElementById("frames").children;
		for (var i = 0; i < el.length; i++) {
			el[i].classList.remove("parent");
			el[i].classList.remove("hide");
			zoom_reset(el[i]);
			if (!dont_update_text) update_text(el[i]);
		}
		search();
	}
	function clearzoom() {
		pinnedDetails = null;
		unzoom();
		var params = get_params();
		if (params.x) delete params.x;
		if (params.y) delete params.y;
		history.replaceState(null, null, parse_params(params));
	}
	function toggle_ignorecase() {
		ignorecase = !ignorecase;
		ignorecaseBtn.classList.toggle("show", ignorecase);
		reset_search();
		search();
	}
	function reset_search() {
		var el = document.querySelectorAll("#frames rect");
		for (var i = 0; i < el.length; i++) orig_load(el[i], "fill");
		var params = get_params();
		delete params.s;
		history.replaceState(null, null, parse_params(params));
	}
	function search_prompt() {
		if (!searching) {
			var term = prompt("Enter a search term (regexp allowed)" + (ignorecase ? ", ignoring case" : "") + "\\nPress Ctrl-i to toggle case sensitivity", "");
			if (term != null) search(term);
		} else {
			reset_search();
			searching = 0;
			currentSearchTerm = null;
			searchbtn.classList.remove("show");
			searchbtn.firstChild.nodeValue = "Search";
			matchedtxt.classList.add("hide");
			matchedtxt.firstChild.nodeValue = "";
		}
	}
	function search(term) {
		if (term) currentSearchTerm = term;
		if (currentSearchTerm === null) return;
		var re = new RegExp(currentSearchTerm, ignorecase ? 'i' : '');
		var el = document.getElementById("frames").children;
		var matches = {};
		var maxwidth = 0;
		for (var i = 0; i < el.length; i++) {
			var e = el[i];
			var func = g_to_func(e);
			var rect = find_child(e, "rect");
			if (func == null || rect == null) continue;
			var w = parseFloat(rect.attributes.width.value);
			if (w > maxwidth) maxwidth = w;
			if (func.match(re)) {
				var x = parseFloat(rect.attributes.x.value);
				orig_save(rect, "fill");
				rect.attributes.fill.value = "%(searchcolor)s";
				matches[x] = (matches[x] === undefined || w > matches[x]) ? w : matches[x];
				searching = 1;
			}
		}
		if (!searching) return;
		var params = get_params();
		params.s = currentSearchTerm;
		history.replaceState(null, null, parse_params(params));
		searchbtn.classList.add("show");
		searchbtn.firstChild.nodeValue = "Reset Search";
		var count = 0, lastx = -1, lastw = 0, keys = [];
		for (var k in matches) { if (matches.hasOwnProperty(k)) keys.push(k); }
		keys.sort(function(a,b){ return a - b; });
		var fudge = 0.0001;
		for (var ki = 0; ki < keys.length; ki++) {
			var x = parseFloat(keys[ki]);
			var w = matches[keys[ki]];
			if (x >= lastx + lastw - fudge) { count += w; lastx = x; lastw = w; }
		}
		matchedtxt.classList.remove("hide");
		var pct = 100 * count / maxwidth;
		if (pct != 100) pct = pct.toFixed(1);
		matchedtxt.firstChild.nodeValue = "Matched: " + pct + "%%";
	}
'''
