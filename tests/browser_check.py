#!/usr/bin/env python3
"""End-to-end check: drive FoamViz in a real browser and screenshot it.

This exists because a Trame app can fail in ways the server never sees -- a
Vue template that will not compile, a state variable the client never receives,
a vtk.js scene that arrives empty. The server-side pipeline tests cannot catch
any of those, so this drives the actual page.

    python tests/browser_check.py --out /tmp/shots
"""

import argparse
import io
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PORT = 8181


def canvas_colours(page):
    """Count distinct colours actually drawn in the 3D view.

    Read from a page screenshot rather than the canvas itself: a WebGL context
    without ``preserveDrawingBuffer`` reads back empty once the frame has been
    presented, so ``drawImage`` on it reports a blank canvas for a view that is
    plainly visible on screen.
    """
    box = page.locator(".foamviz-stage").bounding_box()
    raw = page.screenshot(clip=box)
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    return len(set(image.getdata())), image


def wait_for_render(page, label, minimum=40, timeout=30_000):
    """Wait until the 3D view contains more than just its gradient background."""
    page.wait_for_selector("canvas", timeout=timeout)
    deadline = time.time() + timeout / 1000
    best = 0
    while time.time() < deadline:
        count, _ = canvas_colours(page)
        best = max(best, count)
        if count >= minimum:
            print(f"  [{label}] view has {count} distinct colours")
            return count
        time.sleep(0.5)
    raise AssertionError(
        f"[{label}] view looks empty: only {best} distinct colours "
        f"(expected >= {minimum})"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/tmp/foamviz-shots")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    server = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py"), "--server", "--port", str(PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=ROOT,
    )

    errors, failures = [], []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1600, "height": 950})
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

            for attempt in range(40):
                try:
                    page.goto(f"http://localhost:{PORT}/", timeout=5000)
                    break
                except Exception:
                    time.sleep(0.5)
            else:
                raise AssertionError("server never came up")

            def shot(name):
                page.screenshot(path=str(out / f"{name}.png"))
                print(f"  -> {out / f'{name}.png'}")

            def panel(title, expanded=True):
                """Put a drawer accordion into the wanted state.

                Idempotent on purpose -- some panels start expanded, and a
                blind click on the title would close the very panel the next
                step needs.
                """
                node = page.locator(
                    ".v-expansion-panel", has=page.get_by_text(title, exact=True)
                ).first
                is_open = "v-expansion-panel--active" in (
                    node.get_attribute("class") or ""
                )
                if is_open != expanded:
                    node.locator(".v-expansion-panel-title").click()
                    page.wait_for_timeout(800)

            try:
                # 1. default view: slice through the thermal plume
                wait_for_render(page, "default")
                page.wait_for_timeout(1500)
                shot("01-default")

                # 2. streamlines
                panel("Streamlines")
                page.get_by_label("Show streamlines").check()
                page.wait_for_timeout(5000)
                wait_for_render(page, "streamlines")
                shot("02-streamlines")

                # 3. colour by velocity magnitude instead of temperature.
                # Click the field wrapper, not the input: Vuetify overlays a
                # div that intercepts pointer events on the input itself.
                panel("Colour")
                page.locator(".js-color-field .v-field").click()
                page.wait_for_timeout(600)
                page.get_by_role("option", name="U", exact=True).click()
                page.wait_for_timeout(3500)
                # The legend is driven off the same state the pipeline uses, so
                # it is a fair proxy for "the change actually took effect".
                legend = page.locator(".foamviz-legend-title").inner_text()
                assert legend.startswith("U"), f"legend still shows {legend!r}"
                print(f"  legend now: {legend}")
                wait_for_render(page, "velocity")
                shot("03-velocity")

                # 4. vector arrows on the cut plane
                panel("Streamlines", expanded=False)
                panel("Vector arrows")
                page.get_by_label("Show arrows").check()
                page.wait_for_timeout(3500)
                shot("04-arrows")

                # 5. isosurfaces
                panel("Vector arrows", expanded=False)
                panel("Isosurfaces")
                page.get_by_label("Show isosurfaces").check()
                page.wait_for_timeout(3500)
                shot("05-isosurfaces")

                # 6. server-side rendering must show the same scene
                page.get_by_role("button", name="Server", exact=True).click()
                page.wait_for_timeout(5000)
                wait_for_render(page, "remote")
                shot("06-remote-render")

                # 7. stepping back in time must reload fields without error
                clock = page.locator(".js-time-label")
                before = clock.inner_text()
                # Vuetify's slider is keyboard-driven through its thumb element;
                # the underlying input is not clickable.
                page.locator(".js-time-slider .v-slider-thumb").click()
                for _ in range(5):
                    page.keyboard.press("ArrowLeft")
                    page.wait_for_timeout(1500)
                after = clock.inner_text()
                assert before != after, f"time never advanced (still {before!r})"
                print(f"  time stepped: {before.strip()} -> {after.strip()}")
                wait_for_render(page, "time-step")
                shot("07-earlier-time")

                # 8. switching the colour map must restyle the legend too --
                # the gradient and the geometry come from one sampled table.
                page.get_by_role("button", name="Client", exact=True).click()
                page.wait_for_timeout(2500)
                panel("Isosurfaces", expanded=False)
                panel("Colour")
                gradient_before = page.locator(".foamviz-legend-bar").get_attribute("style")
                page.locator(".js-preset .v-field").click()
                page.wait_for_timeout(600)
                page.get_by_role("option", name="Viridis", exact=True).click()
                page.wait_for_timeout(2500)
                gradient_after = page.locator(".foamviz-legend-bar").get_attribute("style")
                assert gradient_before != gradient_after, "legend gradient did not change"
                print("  colour map switched and legend followed")
                shot("08-viridis")

                # 9. the camera button must deliver a real PNG to the browser
                with page.expect_download(timeout=30_000) as caught:
                    page.locator(".js-screenshot").click()
                download = caught.value
                saved = out / "09-downloaded.png"
                download.save_as(str(saved))
                header = saved.read_bytes()[:8]
                assert header == b"\x89PNG\r\n\x1a\n", f"not a PNG: {header!r}"
                print(f"  download ok: {download.suggested_filename}, "
                      f"{saved.stat().st_size:,} bytes")
            except Exception:
                page.screenshot(path=str(out / "FAILURE.png"))
                print(f"  -> {out / 'FAILURE.png'} (failure state)")
                raise
            finally:
                browser.close()
    except Exception as exc:  # report, do not mask
        failures.append(str(exc))
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    # trame-vtk's client probes POST /paraview/ to discover whether it is
    # talking to a ParaView-backed server. We serve plain VTK, so there is no
    # such route and the 405 is expected -- it comes from the library, not us.
    noise = (
        "favicon",
        "WebSocket is already in CLOSING",
        "ResizeObserver",
        "405",
    )
    real_errors = [e for e in errors if not any(n in e for n in noise)]

    print("\n=== result ===")
    for e in real_errors:
        print("  console error:", e[:300])
    for f in failures:
        print("  FAILURE:", f)
    ok = not real_errors and not failures
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
