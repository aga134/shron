from aiogram import Router


def setup_routers() -> Router:
    from skhron.handlers import (
        favorites,
        feed,
        group,
        inline_mode,
        media_actions,
        menu,
        random_media,
        start,
        upload,
    )
    from skhron.handlers.admin import setup_admin_router

    root = Router(name="root")
    root.include_router(start.router)
    root.include_router(menu.router)
    root.include_router(setup_admin_router())
    root.include_router(media_actions.router)
    root.include_router(random_media.router)
    root.include_router(feed.router)
    root.include_router(favorites.router)
    # group — до upload: его команды ходят только в группах,
    # с private-хендлерами других модулей не конфликтуют
    root.include_router(group.router)
    root.include_router(inline_mode.router)
    # upload — последним: в нём catch-all хендлер на любые присланные медиа
    root.include_router(upload.router)
    return root
