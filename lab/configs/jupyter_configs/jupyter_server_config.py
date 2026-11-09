# Serve the built course documentation inside the JupyterLab UI.
#
# Copied to /root/.jupyter/ by lab/Dockerfile (`COPY lab/configs/jupyter_configs/`).
#
# WHY A PROXY AND NOT THE FILE BROWSER
# Opening a built .html from the JupyterLab file browser goes through Jupyter's
# /files/ endpoint, which sets:
#
#     Content-Security-Policy: frame-ancestors 'self'; ... sandbox allow-scripts
#
# `sandbox` without `allow-same-origin` gives the page an opaque origin. The
# Sphinx theme's first inline <script> is:
#
#     document.documentElement.dataset.mode  = localStorage.getItem("mode")  || "";
#     document.documentElement.dataset.theme = localStorage.getItem("theme") || "";
#
# localStorage access throws SecurityError in an opaque origin, so data-theme is
# never set and the entire NVIDIA theme stylesheet (which keys off those
# attributes) never applies. The page loads, unstyled. A proxied route is
# same-origin with JupyterLab and carries no CSP, so it renders correctly.
#
# WHY NO nginx CHANGE IS NEEDED
# The lab starts Jupyter with `--NotebookApp.base_url="/lab"` (lab/entrypoint.sh),
# so this route is served at /lab/course-docs/. task1/nginx.conf already proxies
# `location /lab` to lab:8888, and nginx matches the LONGEST prefix, so
# /lab/course-docs/ reaches Jupyter. A top-level /course-docs/ route would need
# its own nginx location: this nginx.conf has no catch-all, and answers any
# unclaimed path with 404.

# Where the built HTML lands. Delivered by the S3 data loader into the
# course_data volume; see task1/data_sources.
#
# WHAT PART 1 NEEDS FROM THE SITE (2026-11-04)
# VSS Coding Agent Skills, the last step of Part 1, is a page of this site
# (docs/course/vss-coding-agent-skills.md), and the lab context file the
# coding agent reads is served from it too: docs/_static/vss-lab-context.txt
# lands at /dli/task/data/course-docs/_static/vss-lab-context.txt. The docs
# line in task1/data_sources is active since 2026-11-05, when the site was
# uploaded. A lab launched before that serves an empty directory here.
DOCS_ROOT = "/dli/task/data/course-docs"

c.ServerProxy.servers = {
    "course-docs": {
        # `mkdir -p` first: the data loader may still be copying when a student
        # opens the tile, and `http.server --directory` on a missing path exits
        # immediately, which surfaces as a proxy error rather than an empty
        # page. Creating it keeps the route alive; the docs appear once the
        # copy lands, on refresh.
        "command": [
            "sh", "-c",
            f"mkdir -p {DOCS_ROOT} && exec python3 -m http.server {{port}} "
            f"--bind 127.0.0.1 --directory {DOCS_ROOT}",
        ],
        # The site is built with relative asset paths, so it works unchanged
        # under a URL prefix; server-proxy strips the prefix before forwarding.
        "absolute_url": False,
        "timeout": 30,
        "launcher_entry": {
            "enabled": True,
            "title": "Course Docs",
        },
        # False = open in a JupyterLab tab rather than a new browser tab.
        "new_browser_tab": False,
    }
}
