# Безопасность

## Сообщить об уязвимости

Не открывайте публичный issue. Используйте приватное сообщение через GitHub:
вкладка **Security → Report a vulnerability** этого репозитория.

Особенно интересны: любой путь от fork / pull request к облачным учётным данным или production,
обход ограничений Workload Identity Federation, учётные данные в истории Git.

## Как устроена граница

- Облачный доступ из GitHub Actions — только через OIDC (Workload Identity Federation), без JSON-ключей.
  Привилегированные сервисные аккаунты привязаны к конкретным файлам workflows на `main`.
- Секреты маркетплейсов живут в Google Secret Manager и в репозиторий не попадают.
- Политика публичного репозитория: `docs/security/PUBLIC_REPOSITORY_POLICY.md`.
