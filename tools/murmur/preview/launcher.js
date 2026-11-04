/* Mattermost owns registered menu cleanup when this companion is disabled. */
(() => {
    const app = "/plugins/murmur-preview/public/app/app.html";
    window.registerPlugin("murmur-preview", {
        initialize(registry) {
            registry.registerMainMenuAction("Open Murmur reader preview", () => {
                window.location.assign(app);
            });
        },
    });
})();
