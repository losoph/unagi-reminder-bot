"""screen_registry.py — Реестр обработчиков экранов FSM (RS1).

Заменяет монолитную цепочку if/elif в render_screen на расширяемый реестр экранов.
Обеспечивает регистрацию обработчиков по имени экрана, диспетчеризацию с валидацией
сигнатуры и вызов fallback-обработчика по умолчанию.
"""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable

from aiogram import types
from aiogram.fsm.context import FSMContext

# Type alias for screen handler callable
ScreenHandler = Callable[..., Awaitable[None]]


class ScreenRegistry:
    """Реестр обработчиков экранов интерфейса бота."""

    def __init__(self, default_handler: ScreenHandler | None = None) -> None:
        self._handlers: dict[str, ScreenHandler] = {}
        self._default_handler: ScreenHandler | None = default_handler

    def register(
        self,
        name: str,
        handler: ScreenHandler | None = None,
    ) -> ScreenHandler | Callable[[ScreenHandler], ScreenHandler]:
        """Зарегистрировать обработчик для экрана с именем `name`.

        Поддерживает вызов как функцию:
            registry.register("kb_tags", handler)
        и как декоратор:
            @registry.register("kb_tags")
            async def handle_kb_tags(chat_id, target, state, args): ...
        """

        def decorator(fn: ScreenHandler) -> ScreenHandler:
            self._handlers[name] = fn
            return fn

        if handler is not None:
            return decorator(handler)
        return decorator

    def unregister(self, name: str) -> ScreenHandler | None:
        """Удалить обработчик экрана из реестра. Возвращает удалённый обработчик или None."""
        return self._handlers.pop(name, None)

    def get(self, name: str) -> ScreenHandler | None:
        """Получить зарегистрированный обработчик экрана по имени."""
        return self._handlers.get(name)

    def has(self, name: str) -> bool:
        """Проверить, зарегистрирован ли экран с данным именем."""
        return name in self._handlers

    def __contains__(self, name: str) -> bool:
        return name in self._handlers

    def __len__(self) -> int:
        return len(self._handlers)

    @property
    def screens(self) -> list[str]:
        """Список всех зарегистрированных имён экранов (в алфавитном порядке)."""
        return sorted(self._handlers.keys())

    def set_default(
        self,
        handler: ScreenHandler | None = None,
    ) -> ScreenHandler | Callable[[ScreenHandler], ScreenHandler]:
        """Установить обработчик по умолчанию (fallback) для неизвестных экранов.

        Поддерживает вызов как метод:
            registry.set_default(fallback_handler)
        и как декоратор:
            @registry.set_default
            async def fallback(chat_id, target, state, args): ...
        """

        def decorator(fn: ScreenHandler) -> ScreenHandler:
            self._default_handler = fn
            return fn

        if handler is not None:
            return decorator(handler)
        return decorator

    @property
    def default_handler(self) -> ScreenHandler | None:
        """Текущий обработчик по умолчанию."""
        return self._default_handler

    def clear(self) -> None:
        """Очистить все зарегистрированные экраны."""
        self._handlers.clear()

    async def _invoke(
        self,
        handler: ScreenHandler,
        chat_id: int,
        target: types.Message | types.CallbackQuery,
        state: FSMContext,
        args: dict[str, Any],
        screen_spec: dict[str, Any],
    ) -> None:
        """Вызвать обработчик экрана с адаптацией аргументов под его сигнатуру."""
        try:
            sig = inspect.signature(handler)
        except (ValueError, TypeError):
            # C-extensions, builtins, partials or mocks without signature
            await handler(chat_id, target, state, args)
            return

        params = list(sig.parameters.values())
        param_names = [p.name for p in params]
        has_var_args = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
        has_var_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)

        # Support handlers expecting explicit parameter names including 'screen'
        if "screen" in param_names:
            call_kwargs: dict[str, Any] = {}
            for p in params:
                if p.name == "chat_id":
                    call_kwargs["chat_id"] = chat_id
                elif p.name in ("target", "message", "callback"):
                    call_kwargs[p.name] = target
                elif p.name == "state":
                    call_kwargs["state"] = state
                elif p.name == "screen":
                    call_kwargs["screen"] = screen_spec
                elif p.name == "args":
                    call_kwargs["args"] = args
            if len(call_kwargs) == len(params):
                await handler(**call_kwargs)
                return

        # Positional parameter count matching
        if len(params) == 3 and not has_var_args:
            await handler(chat_id, target, state)
        elif len(params) == 4 and not has_var_kwargs:
            await handler(chat_id, target, state, args)
        elif has_var_kwargs:
            # Accepts **kwargs: pass extracted args
            await handler(chat_id, target, state, **args)
        else:
            await handler(chat_id, target, state, args)

    async def render(
        self,
        chat_id: int,
        screen: dict[str, Any] | str | None,
        target: types.Message | types.CallbackQuery,
        state: FSMContext,
    ) -> None:
        """Отрендерить экран `screen` для пользователя `chat_id`."""
        if isinstance(screen, dict):
            screen_name = screen.get("screen")
            raw_args = screen.get("args")
            args = raw_args if isinstance(raw_args, dict) else {}
            screen_spec = screen
        elif isinstance(screen, str):
            screen_name = screen
            args = {}
            screen_spec = {"screen": screen, "args": {}}
        else:
            screen_name = None
            args = {}
            screen_spec = {}

        if screen_name and screen_name in self._handlers:
            handler = self._handlers[screen_name]
            await self._invoke(handler, chat_id, target, state, args, screen_spec)
            return

        if self._default_handler is not None:
            await self._invoke(self._default_handler, chat_id, target, state, args, screen_spec)
            return

        raise KeyError(
            f"Screen {screen_name!r} is not registered in ScreenRegistry and no default handler is configured."
        )


# Global default registry instance
default_registry = ScreenRegistry()
screen_registry = default_registry

# Module-level convenience functions
register_screen = default_registry.register
unregister_screen = default_registry.unregister
get_screen_handler = default_registry.get
has_screen = default_registry.has
set_default_screen = default_registry.set_default
render_screen = default_registry.render
