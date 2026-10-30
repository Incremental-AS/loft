/* Loft site: theme choice, mobile menu, copy buttons, the language picker, and the language on /pair. No tracking, no cookies, no network.
   /pair carries the pairing details in its URL fragment: nothing here reads location.hash, and it must stay that way. */
(function () {
  var KEY = "loft-theme";              // "light" | "dark"; absent = follow the system
  var root = document.documentElement;
  root.classList.add("js");

  function stored() {
    try { var v = localStorage.getItem(KEY); return v === "light" || v === "dark" ? v : "system"; }
    catch (e) { return "system"; }
  }

  // <picture> media queries can't see data-theme, so dark <source>s are rewritten to match the choice.
  // Each dark source keeps its width condition in data-mq.
  function syncPictures(choice) {
    var sources = document.querySelectorAll('source[data-scheme="dark"]');
    for (var i = 0; i < sources.length; i++) {
      var mq = sources[i].getAttribute("data-mq");
      var media;
      if (choice === "dark") media = mq || "all";
      else if (choice === "light") media = "not all";
      else media = "(prefers-color-scheme: dark)" + (mq ? " and " + mq : "");
      sources[i].media = media;
    }
  }

  function apply(choice) {
    if (choice === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", choice);
    root.setAttribute("data-theme-choice", choice);
    var btn = document.querySelector(".theme-toggle");
    if (btn) {
      var label = btn.getAttribute("data-label-" + choice);
      btn.setAttribute("aria-label", label);
      btn.setAttribute("title", label);
    }
    syncPictures(choice);
  }

  // Before first paint: set data-theme so the colours don't flash.
  var choice = stored();
  if (choice !== "system") root.setAttribute("data-theme", choice);
  root.setAttribute("data-theme-choice", choice);

  // /pair has no language prefix, so it carries every language (<html data-langs="en nb de ...">, blocks marked
  // data-lang) and shows the best match for the browser's languages, English if none match. Without JavaScript,
  // English shows and the other languages are <details> to open.
  var langs = (root.getAttribute("data-langs") || "").split(" ").filter(Boolean);
  function primary(tag) {
    var p = tag.toLowerCase().split("-")[0];
    return p === "nn" || p === "no" ? "nb" : p;       // Norwegian: the one written form we have is bokmål
  }
  function bestLang(wanted) {
    for (var i = 0; i < wanted.length; i++) {
      var w = String(wanted[i] || "").replace(/_/g, "-").toLowerCase();
      if (!w) continue;
      for (var j = 0; j < langs.length; j++) if (langs[j].toLowerCase() === w) return langs[j];
      for (j = 0; j < langs.length; j++) if (primary(langs[j]) === primary(w)) return langs[j];
    }
    return "en";
  }
  function setLang(l) {
    root.setAttribute("data-lang-choice", l);
    root.lang = l;
    if (!document.body) return;           // before the body exists the CSS hides every block (.js [data-lang])
    var blocks = document.querySelectorAll("[data-lang]");
    for (var i = 0; i < blocks.length; i++) {
      var on = blocks[i].getAttribute("data-lang") === l;
      blocks[i].classList.toggle("is-current", on);
      blocks[i].hidden = !on;
      if (on && blocks[i].tagName === "DETAILS") blocks[i].open = true;
    }
    var choices = document.querySelectorAll(".lang-choice");
    for (i = 0; i < choices.length; i++) {
      if (choices[i].getAttribute("data-to") === l) {
        choices[i].setAttribute("aria-current", "true");
        var cur = document.querySelector(".lang-current");
        if (cur) { cur.textContent = choices[i].textContent; cur.lang = l; }
      } else choices[i].removeAttribute("aria-current");
    }
    var t = document.body.getAttribute("data-title-" + l);
    if (t) document.title = t;
  }
  var multilingual = langs.length > 1;
  if (multilingual) setLang(bestLang(navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language]));

  // The language picker is a <details>: it works without JavaScript; this only closes it on Escape or a click outside.
  function closePickers(except, focus) {
    var open = document.querySelectorAll("details.lang-picker[open]");
    for (var i = 0; i < open.length; i++) {
      if (open[i] === except) continue;
      open[i].open = false;
      if (focus) open[i].querySelector("summary").focus();
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    apply(choice);

    if (multilingual) {
      setLang(root.getAttribute("data-lang-choice"));
      var choices = document.querySelectorAll(".lang-choice");
      for (var c = 0; c < choices.length; c++) choices[c].addEventListener("click", function (e) {
        setLang(e.currentTarget.getAttribute("data-to"));
        closePickers(null, true);
      });
    }

    document.addEventListener("click", function (e) {
      var inside = e.target.closest ? e.target.closest("details.lang-picker") : null;
      closePickers(inside, false);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closePickers(null, true);
    });

    var btn = document.querySelector(".theme-toggle");
    if (btn) btn.addEventListener("click", function () {
      choice = { system: "light", light: "dark", dark: "system" }[choice];
      try { if (choice === "system") localStorage.removeItem(KEY); else localStorage.setItem(KEY, choice); }
      catch (e) { /* private mode: the choice lasts for this page only */ }
      apply(choice);
    });

    var menu = document.querySelector(".menu-toggle");
    if (menu) menu.addEventListener("click", function () {
      var open = menu.getAttribute("aria-expanded") !== "true";
      menu.setAttribute("aria-expanded", String(open));
      root.classList.toggle("menu-open", open);
    });

    var blocks = document.querySelectorAll("pre[data-copy]");
    for (var i = 0; i < blocks.length; i++) {
      (function (pre) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "copy";
        b.textContent = pre.getAttribute("data-copy");
        b.addEventListener("click", function () {
          var text = pre.querySelector("code").textContent;
          navigator.clipboard.writeText(text).then(function () {
            var was = b.textContent;
            b.textContent = pre.getAttribute("data-copied");
            setTimeout(function () { b.textContent = was; }, 1600);
          });
        });
        pre.appendChild(b);
      })(blocks[i]);
    }
  });
})();
