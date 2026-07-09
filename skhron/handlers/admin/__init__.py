from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from skhron.filters import AdminFilter
from skhron.keyboards.callbacks import (
    AdminCB,
    CatAdminCB,
    ChatAdminCB,
    InviteCB,
    MenuCB,
    UserAdminCB,
)


def setup_admin_router() -> Router:
    from skhron.handlers.admin import (
        categories,
        groups,
        invites,
        panel,
        stats_backup,
        users,
    )

    admin = Router(name="admin")
    admin.message.filter(AdminFilter())
    admin.callback_query.filter(AdminFilter())
    admin.include_router(panel.router)
    admin.include_router(categories.router)
    admin.include_router(users.router)
    admin.include_router(groups.router)
    admin.include_router(invites.router)
    admin.include_router(stats_backup.router)

    denied = Router(name="admin_denied")

    @denied.callback_query(MenuCB.filter(F.action == "admin"))
    @denied.callback_query(AdminCB.filter())
    @denied.callback_query(CatAdminCB.filter())
    @denied.callback_query(UserAdminCB.filter())
    @denied.callback_query(ChatAdminCB.filter())
    @denied.callback_query(InviteCB.filter())
    async def admin_access_denied(callback: CallbackQuery) -> None:
        await callback.answer("Доступ закрыт: нужны права админа 🙅", show_alert=True)

    @denied.message(
        Command("admin", "rehash", "rehash_stop"), F.chat.type == "private"
    )
    async def admin_command_denied(message: Message) -> None:
        # у бывшего админа /admin не должен умирать молча
        await message.answer("Доступ закрыт: нужны права админа 🙅")

    root = Router(name="admin_root")
    root.include_router(admin)
    # denied — после admin: срабатывает, только если AdminFilter не пропустил,
    # так что у бывшего админа протухшие кнопки не крутятся вечным спиннером
    root.include_router(denied)
    return root
