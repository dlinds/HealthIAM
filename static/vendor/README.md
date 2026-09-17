Vendored front-end assets (no build step):

- Bootstrap 5.3.3 (MIT) — https://getbootstrap.com
- Bootstrap Icons 1.11.3 (MIT) — https://icons.getbootstrap.com
- htmx 2.0.3 (0BSD) — https://htmx.org

To upgrade, download the dist files for the new version and replace these files.

After replacing Bootstrap, strip the trailing `sourceMappingURL` comments:

    sed -i '/^\/\/# sourceMappingURL=/d' bootstrap/bootstrap.bundle.min.js
    sed -i '/^\/\*# sourceMappingURL=.*\*\/$/d' bootstrap/bootstrap.min.css

The `.map` files are not vendored, and whitenoise's manifest storage fails the
`collectstatic` build step on a reference it cannot resolve.
