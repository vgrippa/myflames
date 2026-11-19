import{bK as p}from"./index-bT8Q4hbJ.js";const b="--mfc-canvas:#fafafa;--mfc-card:#fff;--mfc-pane:#f2f2f4;--mfc-ink:#243341;--mfc-muted:#596a79;--mfc-line:#d8e0e5;--mfc-boundary:#82858c;--mfc-focus:#007aff;--mfc-link:#0063cc;--mfc-hover:rgba(0,0,0,.04);--mfc-selected:#eaf3ff;--mfc-danger:#b42318;--mfc-edge:#8795a1;--mfc-mark-ink:#18222c;--mfc-venn:#f5f5f7;--mfc-venn-on:#b7d9ff;--mfc-venn-ring:#6e7d8c;--mfc-estimate:#5b3cc4;",h="--mfc-canvas:#151719;--mfc-card:#25282c;--mfc-pane:#1c1e21;--mfc-ink:#ededee;--mfc-muted:#a5a9af;--mfc-line:#34383d;--mfc-boundary:#7b8592;--mfc-focus:#0a84ff;--mfc-link:#409cff;--mfc-hover:rgba(255,255,255,.06);--mfc-selected:#20354e;--mfc-danger:#ff8b82;--mfc-edge:#a0aeba;--mfc-venn:#3a3f45;--mfc-venn-on:#2d5f96;--mfc-venn-ring:#8b97a3;--mfc-estimate:#b3a4ff;",g={light:Object.fromEntries(b.split(";").filter(Boolean).map(e=>e.split(":"))),dark:Object.fromEntries(h.split(";").filter(Boolean).map(e=>e.split(":")))},l="myflames-appearance",i=["--accent",..."text fill hover soft line ink".split(" ").map(e=>"--accent-"+e),"--focus"],d="canvas card pane ink muted line boundary hover selected focus link danger edge venn venn-on venn-ring estimate".split(" ").map(e=>"--mfc-"+e);function y(e){const t=`<style>
    button.primary { background:var(--accent-fill,var(--accent)); border-color:var(--accent-fill,var(--accent)); }
    button.primary:hover:not(:disabled) { background:var(--accent-hover); border-color:var(--accent-hover); }
    button[aria-pressed="true"], a { color:var(--accent-text,var(--accent)); }
  </style>`,a=`<script>(function(){
    function validColor(value){
      if(typeof value!=="string" || value.length>128 || value.includes("\\\\")) return false;
      var hex=/^#(?:[0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i;
      var fn=/^(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch|color|color-mix)\\([0-9a-z.,%/ ()+-]+\\)$/i;
      return (hex.test(value) || fn.test(value)) && !/\\b(?:var|attr|env|if|url|from)\\b/i.test(value) && CSS.supports("color",value);
    }
    window.addEventListener("message",function(event){
      var data=event.data;
      if(event.source!==parent || !data || data.type!=="${l}" || (data.theme!=="light" && data.theme!=="dark")) return;
      var root=document.documentElement;
      root.setAttribute("data-theme",data.theme);
      root.style.colorScheme=data.theme;
      var names=${JSON.stringify(i)};
      names.forEach(function(name){
        var value=data.accents?.[name];
        if(validColor(value)) root.style.setProperty(name,value);
      });
      var surfaces=${JSON.stringify(d)};
      surfaces.forEach(function(name){
        var value=data.surfaces?.[name];
        if(validColor(value)) root.style.setProperty(name,value);
      });
    });
  })();<\/script>`;return e.replace(/<\/head\s*>/i,t+a+"</head>")}function k(e){const t=e.ownerDocument,a=t.defaultView;if(!a)return()=>{};const n=()=>{const u=a.getComputedStyle(t.documentElement),f=p(t.documentElement),s={};i.forEach(c=>{s[c]=u.getPropertyValue(c).trim()});const v=a.getComputedStyle(t.querySelector(".workspace")||t.documentElement),m={};d.forEach(c=>{m[c]=v.getPropertyValue(c).trim()||g[f][c]}),e.contentWindow?.postMessage({type:l,theme:f,accents:s,surfaces:m},"*")},r=new MutationObserver(n);r.observe(t.documentElement,{attributes:!0,attributeFilter:["data-theme"]});const o=a.matchMedia("(prefers-color-scheme: dark)");return o.addEventListener("change",n),e.addEventListener("load",n),n(),()=>{r.disconnect(),o.removeEventListener("change",n),e.removeEventListener("load",n)}}export{h as D,b as L,k as b,y as t};
