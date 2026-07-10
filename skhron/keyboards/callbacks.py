"""Все CallbackData-фабрики проекта — единый реестр, чтобы не пересекались
префиксы и модули могли ссылаться на кнопки друг друга."""

from aiogram.filters.callback_data import CallbackData


class MenuCB(CallbackData, prefix="menu"):
    # home | random | feed | favorites | liked | upload | access | admin | help
    action: str


class RandomCB(CallbackData, prefix="rnd"):
    # 0 = из всех доступных категорий
    category_id: int


class FeedPickCB(CallbackData, prefix="fpick"):
    category_id: int


class FeedCB(CallbackData, prefix="feed"):
    # offset=-1 — клик по счётчику «N/M»: открыть ввод номера поста
    category_id: int
    offset: int


class FavPageCB(CallbackData, prefix="favp"):
    # offset=-1 — клик по счётчику: открыть ввод номера поста
    offset: int


class MediaActionCB(CallbackData, prefix="ma"):
    # fav — в/из избранного; del — запрос удаления;
    # delc — подтвердить удаление; delx — отменить удаление;
    # cap — изменить подпись; capx — отменить изменение подписи
    action: str
    media_id: int


class UploadPickCB(CallbackData, prefix="upick"):
    # выбор категории в потоке «из меню» (включая смену на лету в collecting)
    category_id: int


class UploadPendingPickCB(CallbackData, prefix="upickp"):
    # выбор категории для уже присланных файлов (поток «сначала кинул медиа»);
    # отдельная фабрика, чтобы протухшая кнопка не включала молча режим collecting
    category_id: int


class UploadDoneCB(CallbackData, prefix="updone"):
    pass


class AdminCB(CallbackData, prefix="adm"):
    # home | cats | users | invites | stats | backup
    section: str


class CatAdminCB(CallbackData, prefix="admc"):
    # list — список категорий (page); new — создать;
    # open — карточка категории; ren — переименовать; arch — архив вкл/выкл;
    # del — запрос удаления; delc — подтвердить удаление;
    # users — доступы категории; adduser — выдать доступ
    action: str
    category_id: int = 0
    page: int = 0


class UserAdminCB(CallbackData, prefix="admu"):
    # list — список юзеров (page); open — карточка юзера;
    # tadmin — переключить админку; grant — выбрать категорию для выдачи;
    # pview / pupload — переключить право в категории; revoke — отозвать доступ
    action: str
    user_id: int = 0
    category_id: int = 0
    page: int = 0


class DupCB(CallbackData, prefix="dup"):
    # вопрос «похоже на дубль — сохранить всё равно?»
    # save — сохранить; skip — не сохранять. key — ключ файла в FSM data
    action: str
    key: str


class GroupRandomCB(CallbackData, prefix="grnd"):
    # рандом ИЗ ГРУППЫ: права проверяются по chat_id сообщения;
    # 0 = из всех разрешённых этой группе категорий
    category_id: int


class GroupLikeCB(CallbackData, prefix="glike"):
    # лайк под медиа-постом бота В ГРУППЕ: тоггл per (user, media),
    # счётчик на кнопке; права проверяются по chat_id сообщения
    media_id: int


class LikedPageCB(CallbackData, prefix="likedp"):
    # личная лента лайкнутого; offset=-1 — ввод номера, -2 — отмена ввода
    offset: int


class GroupSaveCB(CallbackData, prefix="gsave"):
    # выбор категории для /save в группе (кнопки жмёт только автор команды)
    category_id: int


class DailyCB(CallbackData, prefix="admd"):
    # «мем дня» в админ-карточке группы: minutes — время в минутах
    # от полуночи (DISPLAY_TZ), -1 — выключить
    chat_id: int
    minutes: int


class GroupFeedPickCB(CallbackData, prefix="gfpick"):
    # выбор категории для ленты в группе
    category_id: int


class GroupFeedCB(CallbackData, prefix="gfeed"):
    # лента В ГРУППЕ: права по chat_id сообщения;
    # offset=-1 — клик по счётчику: открыть ввод номера поста
    category_id: int
    offset: int


class ChatAdminCB(CallbackData, prefix="admg"):
    # list — список групп; open — карточка группы;
    # toggle — разрешить/запретить категорию группе; forget — забыть группу
    action: str
    chat_id: int = 0
    category_id: int = 0


class InviteCB(CallbackData, prefix="inv"):
    # list — список; new — начать создание; cat — переключить категорию
    # в черновике; rights — переключить права; uses — выбрать лимит (value);
    # create — сгенерировать; open — карточка; off — деактивировать
    action: str
    invite_id: int = 0
    value: int = 0
