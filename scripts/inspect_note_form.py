"""Deeper inspection of the saved note-form HTML: locate the controls my
first pass missed (Subject input, Interaction Type combobox, rich-text
editor) and figure out how to scope to a single active tab."""
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup


def describe(el, keys=None):
    if el is None:
        return "<none>"
    keys = keys or ("id", "name", "class", "type", "role", "aria-label",
                    "aria-hidden", "aria-selected", "data-aura-class",
                    "data-tab-value", "data-tab-name", "for", "placeholder",
                    "contenteditable")
    attrs = {}
    for k in keys:
        if k in el.attrs:
            v = el.attrs[k]
            if isinstance(v, list):
                v = " ".join(v)
            attrs[k] = v
    return f"<{el.name} {attrs}>"


def show_section(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def main(path: str) -> None:
    html = Path(path).read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "lxml")

    # 1) iframes — would force frame switching in Playwright
    show_section("IFRAMES")
    frames = soup.find_all("iframe")
    print(f"  count: {len(frames)}")
    for f in frames[:10]:
        print(" ", describe(f, ("id", "name", "title", "src", "class")))

    # 2) Subject input — find input whose nearest label says "Subject"
    show_section("SUBJECT input (find labels that == 'Subject' or '*Subject')")
    for lbl in soup.find_all("label"):
        text = lbl.get_text(strip=True)
        if text in ("Subject", "*Subject") or text.rstrip("*").strip() == "Subject":
            print(f"  label: {describe(lbl)} text={text!r}")
            inp = soup.find(id=lbl.get("for")) if lbl.get("for") else None
            print(f"    -> input: {describe(inp)}")

    # 3) Interaction Type combobox — find labels matching, then inspect siblings
    show_section("INTERACTION TYPE combobox")
    for lbl in soup.find_all(["label", "span", "legend"]):
        text = lbl.get_text(strip=True)
        if text == "Interaction Type" or text == "*Interaction Type":
            print(f"  label: {describe(lbl)} text={text!r}")
            # Walk up looking for a form-element container
            ancestor = lbl
            for _ in range(8):
                ancestor = ancestor.parent
                if ancestor is None or ancestor.name == "body":
                    break
                # Look for select / combobox / button[role=combobox] inside
                cb = ancestor.find(attrs={"role": "combobox"})
                sel = ancestor.find("select")
                if cb is not None:
                    print(f"    -> combobox role: {describe(cb)}")
                    break
                if sel is not None:
                    print(f"    -> select: {describe(sel)}")
                    break
            else:
                print("    (no combobox/select found nearby)")

    # 4) Rich-text editor — look for contenteditable, role=textbox, lwc-richtext
    show_section("RICH TEXT EDITOR candidates")
    seen = set()
    for el in soup.find_all(attrs={"contenteditable": True}):
        key = (el.name, el.get("class") and " ".join(el.get("class")))
        if key in seen:
            continue
        seen.add(key)
        print(" ", describe(el, ("id", "class", "role", "aria-label",
                                  "contenteditable", "data-aura-class")))
    for el in soup.find_all(attrs={"role": "textbox"}):
        print("  role=textbox:", describe(el))

    # 5) Tab/scope marker — find Lightning workspace tab containers
    show_section("WORKSPACE TABS (active/inactive)")
    # Common Lightning class: oneConsoleTabItem, slds-context-bar__item
    for el in soup.find_all(attrs={"role": "tab"})[:20]:
        print(" ", describe(el, ("id", "class", "role", "aria-selected",
                                  "title", "data-tab-value")))

    # 6) Visibility classes around the note panel
    show_section("NOTE PANEL containers (search 'Create a New Student Note')")
    for txt in soup.find_all(string=re.compile("Create a New Student Note", re.I)):
        ancestor = txt.parent
        for depth in range(12):
            if ancestor is None or ancestor.name == "body":
                break
            cls = ancestor.get("class") or []
            if isinstance(cls, list):
                cls = " ".join(cls)
            hidden = ancestor.get("aria-hidden")
            if "slds-hide" in cls or "slds-show" in cls or hidden:
                print(f"  ancestor depth={depth}: {describe(ancestor)}")
            ancestor = ancestor.parent

    # 7) Submit/Clear buttons in note form
    show_section("SUBMIT / CLEAR buttons")
    for b in soup.find_all("button"):
        text = b.get_text(strip=True)
        if text in ("Submit", "Clear"):
            print(" ", describe(b, ("id", "class", "type", "name", "aria-label")),
                  "text=", repr(text))


if __name__ == "__main__":
    main(sys.argv[1])
