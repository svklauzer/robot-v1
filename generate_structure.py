import os

# --- НАСТРОЙКА ---
# Имя файла, в который будет сохранена структура
OUTPUT_FILENAME = 'project_structure.txt'

# Папки, которые нужно ПОЛНОСТЬЮ проигнорировать
# Добавьте сюда свои папки при необходимости
EXCLUDE_DIRS = {
    # Запрошенные вами
    'docker',
    'alembic',

    # Стандартные для Python и разработки
    '.git',
    '__pycache__',
    '.venv',
    'venv',
    'env',
    'node_modules',
    '.vscode',
    '.idea',
    'dist',
    'build',

    # Папки с версиями миграций (часто внутри alembic, но на всякий случай)
    'versions',
    
    # Кэш тестов
    '.pytest_cache',
    'htmlcov',
}

# Файлы, которые нужно игнорировать
EXCLUDE_FILES = {
    # Системные файлы
    '.DS_Store',
    'Thumbs.db',
    
    # Файлы окружения (часто содержат секреты)
    '.env',
    
    # Сам скрипт
    'generate_structure.py',
    OUTPUT_FILENAME
}

# Расширения файлов, которые нужно игнорировать
EXCLUDE_EXTENSIONS = {
    '.pyc',
    '.log',
    '.sqlite3'
}
# --- КОНЕЦ НАСТРОЙКИ ---


def generate_tree(startpath):
    """Генерирует список строк для древовидной структуры проекта."""
    tree_lines = []
    
    # Преобразуем множества в кортежи для startswith/endswith (быстрее)
    exclude_dirs_tuple = tuple(EXCLUDE_DIRS)
    exclude_files_tuple = tuple(EXCLUDE_FILES)
    exclude_extensions_tuple = tuple(EXCLUDE_EXTENSIONS)

    for root, dirs, files in os.walk(startpath, topdown=True):
        # Исключаем директории из дальнейшего обхода (очень эффективно)
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]

        level = root.replace(startpath, '').count(os.sep)
        indent = ' ' * 4 * level
        
        # Не отображаем корневую папку, так как мы добавим её в начале
        if level > 0:
            tree_lines.append(f'{indent[:-4]}└── {os.path.basename(root)}/')
        
        sub_indent = ' ' * 4 * (level)
        
        # Сортируем файлы для консистентного вывода
        sorted_files = sorted(files)
        
        for f in sorted_files:
            # Пропускаем ненужные файлы и расширения
            if f in exclude_files_tuple or f.endswith(exclude_extensions_tuple):
                continue
            tree_lines.append(f'{sub_indent}├── {f}')
            
    return tree_lines


if __name__ == '__main__':
    # Получаем путь к текущей папке, где запущен скрипт
    project_path = os.getcwd()
    project_name = os.path.basename(project_path)
    
    # Генерируем структуру
    tree_output_lines = generate_tree(project_path)
    
    # Открываем файл для записи
    try:
        with open(OUTPUT_FILENAME, 'w', encoding='utf-8') as f:
            # Записываем название корневой папки
            f.write(f'{project_name}/\n')
            # Записываем остальную структуру
            f.write('\n'.join(tree_output_lines))
        
        print(f"✅ Структура проекта успешно сохранена в файл: {OUTPUT_FILENAME}")
        
    except IOError as e:
        print(f"❌ Ошибка при записи в файл: {e}")