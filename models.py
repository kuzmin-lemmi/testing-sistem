"""
Модели базы данных SQLite
"""
import sqlite3
from datetime import datetime, timezone
from config import DATABASE_PATH

def get_db():
    """Получить соединение с БД (с настройками для многопоточного Flask)"""
    # timeout помогает избежать 'database is locked' при частых автосохранениях
    conn = sqlite3.connect(DATABASE_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # Праги для устойчивой работы в кабинете (много чтений + частые записи)
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA synchronous = NORMAL')
    conn.execute('PRAGMA busy_timeout = 5000')
    return conn

def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())

def migrate_db():
    """Миграции схемы БД для уже существующих установок."""
    conn = get_db()
    cur = conn.cursor()

    def _fk_refs(table: str):
        try:
            return [tuple(r) for r in cur.execute(f"PRAGMA foreign_key_list('{table}')").fetchall()]
        except Exception:
            return []

    def _table_exists(name: str) -> bool:
        r = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return bool(r)

    def _rebuild_variant_dependent_tables():
        """Recreate tables that FK-reference variants.

        This prevents broken foreign keys pointing to variants_old after the variants migration.
        """
        # variant_tasks
        if _table_exists('variant_tasks'):
            cur.execute('''
                CREATE TABLE IF NOT EXISTS variant_tasks_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    variant_id INTEGER NOT NULL,
                    task_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    FOREIGN KEY (variant_id) REFERENCES variants(id) ON DELETE CASCADE,
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                )
            ''')
            cur.execute('''
                INSERT INTO variant_tasks_new (id, variant_id, task_id, position)
                SELECT id, variant_id, task_id, position FROM variant_tasks
            ''')
            cur.execute('DROP TABLE variant_tasks')
            cur.execute('ALTER TABLE variant_tasks_new RENAME TO variant_tasks')

        # test_sessions
        if _table_exists('test_sessions'):
            # In older DBs teacher_finish_only may not exist yet
            cols = [r[1] for r in cur.execute("PRAGMA table_info('test_sessions')").fetchall()]
            has_tfo = 'teacher_finish_only' in cols
            cur.execute('''
                CREATE TABLE IF NOT EXISTS test_sessions_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    variant_id INTEGER,
                    individual_mode BOOLEAN NOT NULL DEFAULT 0,
                    time_limit INTEGER NOT NULL,
                    access_code TEXT,
                    show_answers BOOLEAN NOT NULL DEFAULT 1,
                    teacher_finish_only BOOLEAN NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'closed')),
                    grade_5_min INTEGER,
                    grade_4_min INTEGER,
                    grade_3_min INTEGER,
                    total_tasks INTEGER,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (variant_id) REFERENCES variants(id)
                )
            ''')
            if has_tfo:
                cur.execute('''
                    INSERT INTO test_sessions_new (id, variant_id, individual_mode, time_limit, access_code, show_answers, teacher_finish_only,
                                                  status, grade_5_min, grade_4_min, grade_3_min, total_tasks, created_at)
                    SELECT id, variant_id, individual_mode, time_limit, access_code, show_answers, teacher_finish_only,
                           status, grade_5_min, grade_4_min, grade_3_min, total_tasks, created_at
                    FROM test_sessions
                ''')
            else:
                cur.execute('''
                    INSERT INTO test_sessions_new (id, variant_id, individual_mode, time_limit, access_code, show_answers, teacher_finish_only,
                                                  status, grade_5_min, grade_4_min, grade_3_min, total_tasks, created_at)
                    SELECT id, variant_id, individual_mode, time_limit, access_code, show_answers, 0,
                           status, grade_5_min, grade_4_min, grade_3_min, total_tasks, created_at
                    FROM test_sessions
                ''')
            cur.execute('DROP TABLE test_sessions')
            cur.execute('ALTER TABLE test_sessions_new RENAME TO test_sessions')

        # students
        if _table_exists('students'):
            cols = [r[1] for r in cur.execute("PRAGMA table_info('students')").fetchall()]
            has_last_seen = 'last_seen_at' in cols
            cur.execute('''
                CREATE TABLE IF NOT EXISTS students_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    first_name TEXT NOT NULL,
                    last_name TEXT NOT NULL,
                    variant_id INTEGER NOT NULL,
                    started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    finished_at DATETIME,
                    status TEXT NOT NULL DEFAULT 'in_progress' CHECK(status IN ('in_progress', 'finished')),
                    last_seen_at DATETIME,
                    FOREIGN KEY (session_id) REFERENCES test_sessions(id) ON DELETE CASCADE,
                    FOREIGN KEY (variant_id) REFERENCES variants(id)
                )
            ''')
            if has_last_seen:
                cur.execute('''
                    INSERT INTO students_new (id, session_id, first_name, last_name, variant_id, started_at, finished_at, status, last_seen_at)
                    SELECT id, session_id, first_name, last_name, variant_id, started_at, finished_at, status, last_seen_at
                    FROM students
                ''')
            else:
                cur.execute('''
                    INSERT INTO students_new (id, session_id, first_name, last_name, variant_id, started_at, finished_at, status, last_seen_at)
                    SELECT id, session_id, first_name, last_name, variant_id, started_at, finished_at, status, NULL
                    FROM students
                ''')
            cur.execute('DROP TABLE students')
            cur.execute('ALTER TABLE students_new RENAME TO students')

    # 0) Repair broken FKs to variants_old from older migrations
    fk_tables = ('variant_tasks', 'test_sessions', 'students')
    if any(any(r[2] == 'variants_old' for r in _fk_refs(t)) for t in fk_tables):
        conn.execute('PRAGMA foreign_keys = OFF')
        conn.execute('BEGIN')
        try:
            _rebuild_variant_dependent_tables()
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            raise
        finally:
            conn.execute('PRAGMA foreign_keys = ON')

    # 1) variants: расширяем допустимые типы (thematic/full + mixed/uploaded)
    cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='variants'")
    row = cur.fetchone()
    if row and row['sql']:
        sql = row['sql']
        if "variant_type IN ('thematic', 'full')" in sql:
            conn.execute('PRAGMA foreign_keys = OFF')
            conn.execute('BEGIN')
            try:
                cur.execute("ALTER TABLE variants RENAME TO variants_old")
                cur.execute('''
                    CREATE TABLE variants (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        variant_type TEXT NOT NULL CHECK(variant_type IN ('thematic', 'full', 'mixed', 'uploaded')),
                        ege_number INTEGER,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                cur.execute('''
                    INSERT INTO variants (id, name, variant_type, ege_number, created_at)
                    SELECT id, name,
                           CASE
                             WHEN variant_type IN ('thematic','full','mixed','uploaded') THEN variant_type
                             ELSE 'full'
                           END,
                           ege_number, created_at
                    FROM variants_old
                ''')

                # Rebuild dependent tables so their FK points to the new variants
                _rebuild_variant_dependent_tables()

                cur.execute("DROP TABLE variants_old")
                conn.execute('COMMIT')
            except Exception:
                conn.execute('ROLLBACK')
                raise
            finally:
                conn.execute('PRAGMA foreign_keys = ON')

    # 2) students: last_seen_at (для отслеживания активности)
    if not _table_has_column(conn, 'students', 'last_seen_at'):
        cur.execute("ALTER TABLE students ADD COLUMN last_seen_at DATETIME")
        cur.execute("UPDATE students SET last_seen_at = COALESCE(last_seen_at, started_at)")

    # 2b) students: сколько секунд паузы уже накопилось у сессии к моменту входа ученика.
    # Пауза, случившаяся до его входа, не должна прибавлять ему время.
    if not _table_has_column(conn, 'students', 'pause_base_seconds'):
        cur.execute("ALTER TABLE students ADD COLUMN pause_base_seconds INTEGER NOT NULL DEFAULT 0")

    # 2c) answers: одна запись на пару «ученик — задание».
    # Раньше два одновременных сохранения могли создать две записи, и верный ответ засчитывался дважды.
    cur.execute('''
        DELETE FROM answers
        WHERE id NOT IN (SELECT MAX(id) FROM answers GROUP BY student_id, task_id)
    ''')
    cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_answers_student_task ON answers(student_id, task_id)')

    # 3) answers: answer_text (для задач с текстовым/многострочным ответом)
    if not _table_has_column(conn, 'answers', 'answer_text'):
        cur.execute("ALTER TABLE answers ADD COLUMN answer_text TEXT")

    # 4) test_sessions: teacher_finish_only
    if not _table_has_column(conn, 'test_sessions', 'teacher_finish_only'):
        cur.execute("ALTER TABLE test_sessions ADD COLUMN teacher_finish_only BOOLEAN NOT NULL DEFAULT 0")

    # 5) test_sessions: pause/extend controls
    if not _table_has_column(conn, 'test_sessions', 'paused'):
        cur.execute("ALTER TABLE test_sessions ADD COLUMN paused BOOLEAN NOT NULL DEFAULT 0")
    if not _table_has_column(conn, 'test_sessions', 'paused_at'):
        cur.execute("ALTER TABLE test_sessions ADD COLUMN paused_at DATETIME")
    if not _table_has_column(conn, 'test_sessions', 'pause_total_seconds'):
        cur.execute("ALTER TABLE test_sessions ADD COLUMN pause_total_seconds INTEGER NOT NULL DEFAULT 0")
    if not _table_has_column(conn, 'test_sessions', 'extra_seconds'):
        cur.execute("ALTER TABLE test_sessions ADD COLUMN extra_seconds INTEGER NOT NULL DEFAULT 0")

    # 6) test_sessions: calculator
    if not _table_has_column(conn, 'test_sessions', 'calculator_enabled'):
        cur.execute("ALTER TABLE test_sessions ADD COLUMN calculator_enabled BOOLEAN NOT NULL DEFAULT 0")

    # 6c) students: «работа сдана» — ответы скрыты и не меняются, оценка появится после завершения тестирования
    if not _table_has_column(conn, 'students', 'submitted_at'):
        cur.execute("ALTER TABLE students ADD COLUMN submitted_at DATETIME")
    # 6d) test_sessions: введённые ответы скрыты точками, пока ученик не нажмёт на поле
    if not _table_has_column(conn, 'test_sessions', 'mask_answers'):
        cur.execute("ALTER TABLE test_sessions ADD COLUMN mask_answers BOOLEAN NOT NULL DEFAULT 0")

    # 6d2) students: сколько раз ученик уходил со страницы теста и сколько секунд там пробыл
    for column, kind in (('leave_count', 'INTEGER NOT NULL DEFAULT 0'), ('leave_seconds', 'INTEGER NOT NULL DEFAULT 0'),
                         ('left_at', 'DATETIME')):
        if not _table_has_column(conn, 'students', column):
            cur.execute(f"ALTER TABLE students ADD COLUMN {column} {kind}")

    # 6e) variants: место в каталоге «Класс → Тема → Комплект» и время на работу
    for column, kind in (('grade', 'TEXT'), ('topic', 'TEXT'), ('kit', 'TEXT'), ('minutes', 'INTEGER')):
        if not _table_has_column(conn, 'variants', column):
            cur.execute(f"ALTER TABLE variants ADD COLUMN {column} {kind}")

    # 6b) test_sessions: задания у каждого ученика идут в своём порядке
    if not _table_has_column(conn, 'test_sessions', 'shuffle_tasks'):
        cur.execute("ALTER TABLE test_sessions ADD COLUMN shuffle_tasks BOOLEAN NOT NULL DEFAULT 0")

    # 7) test_sessions: python editor
    if not _table_has_column(conn, 'test_sessions', 'python_enabled'):
        cur.execute("ALTER TABLE test_sessions ADD COLUMN python_enabled BOOLEAN NOT NULL DEFAULT 0")

    # 8) classes table
    cur.execute('''
        CREATE TABLE IF NOT EXISTS classes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cur.execute("INSERT OR IGNORE INTO classes (name) VALUES ('Общий банк')")

    # 9) tasks: scope + class
    if not _table_has_column(conn, 'tasks', 'task_scope'):
        cur.execute("ALTER TABLE tasks ADD COLUMN task_scope TEXT NOT NULL DEFAULT 'ege'")
    if not _table_has_column(conn, 'tasks', 'class_id'):
        cur.execute("ALTER TABLE tasks ADD COLUMN class_id INTEGER")

    # 10) variants: scope + class
    if not _table_has_column(conn, 'variants', 'variant_scope'):
        cur.execute("ALTER TABLE variants ADD COLUMN variant_scope TEXT NOT NULL DEFAULT 'ege'")
    if not _table_has_column(conn, 'variants', 'class_id'):
        cur.execute("ALTER TABLE variants ADD COLUMN class_id INTEGER")

    # 11) tasks: answer_kind (classic | file_upload)
    if not _table_has_column(conn, 'tasks', 'answer_kind'):
        cur.execute("ALTER TABLE tasks ADD COLUMN answer_kind TEXT NOT NULL DEFAULT 'classic'")

    # 12) answers: uploaded file fields
    if not _table_has_column(conn, 'answers', 'upload_path'):
        cur.execute("ALTER TABLE answers ADD COLUMN upload_path TEXT")
    if not _table_has_column(conn, 'answers', 'upload_name'):
        cur.execute("ALTER TABLE answers ADD COLUMN upload_name TEXT")
    if not _table_has_column(conn, 'answers', 'upload_size'):
        cur.execute("ALTER TABLE answers ADD COLUMN upload_size INTEGER")
    if not _table_has_column(conn, 'answers', 'upload_uploaded_at'):
        cur.execute("ALTER TABLE answers ADD COLUMN upload_uploaded_at DATETIME")

    # 13) students: visible variant label for multi-variant sessions
    if not _table_has_column(conn, 'students', 'variant_label'):
        cur.execute("ALTER TABLE students ADD COLUMN variant_label TEXT")

    # 14) session_variants: list of ready variants available in a session
    cur.execute('''
        CREATE TABLE IF NOT EXISTS session_variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            variant_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            FOREIGN KEY (session_id) REFERENCES test_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (variant_id) REFERENCES variants(id) ON DELETE CASCADE
        )
    ''')

    conn.commit()
    conn.close()


def init_db():
    """Инициализация базы данных"""
    conn = get_db()
    cursor = conn.cursor()

    # Таблица классов (для обычных работ, не ЕГЭ)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS classes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute("INSERT OR IGNORE INTO classes (name) VALUES ('Общий банк')")

    # Таблица задач
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ege_number INTEGER NOT NULL CHECK(ege_number >= 1 AND ege_number <= 27),
            task_scope TEXT NOT NULL DEFAULT 'ege',
            class_id INTEGER,
            image_path TEXT NOT NULL,
            attachment_path TEXT,
            attachment_name TEXT,
            answer_kind TEXT NOT NULL DEFAULT 'classic',
            answer_count INTEGER NOT NULL DEFAULT 1,
            answer_1 INTEGER,
            answer_2 INTEGER,
            answer_text TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (class_id) REFERENCES classes(id)
        )
    ''')

    # Таблица вариантов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            variant_type TEXT NOT NULL CHECK(variant_type IN ('thematic', 'full', 'mixed', 'uploaded')),
            ege_number INTEGER,
            variant_scope TEXT NOT NULL DEFAULT 'ege',
            class_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (class_id) REFERENCES classes(id)
        )
    ''')

    # Таблица связи вариант-задачи
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS variant_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            variant_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            FOREIGN KEY (variant_id) REFERENCES variants(id) ON DELETE CASCADE,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    ''')

    # Таблица тестирований (сессий)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS test_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            variant_id INTEGER,
            individual_mode BOOLEAN NOT NULL DEFAULT 0,
            time_limit INTEGER NOT NULL,
            access_code TEXT,
            show_answers BOOLEAN NOT NULL DEFAULT 1,
            teacher_finish_only BOOLEAN NOT NULL DEFAULT 0,
            paused BOOLEAN NOT NULL DEFAULT 0,
            paused_at DATETIME,
            pause_total_seconds INTEGER NOT NULL DEFAULT 0,
            extra_seconds INTEGER NOT NULL DEFAULT 0,
            calculator_enabled BOOLEAN NOT NULL DEFAULT 0,
            python_enabled BOOLEAN NOT NULL DEFAULT 0,
            shuffle_tasks BOOLEAN NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'closed')),
            grade_5_min INTEGER,
            grade_4_min INTEGER,
            grade_3_min INTEGER,
            total_tasks INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (variant_id) REFERENCES variants(id)
        )
    ''')

    # Таблица учеников на тестировании
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            variant_id INTEGER NOT NULL,
            variant_label TEXT,
            started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            finished_at DATETIME,
            status TEXT NOT NULL DEFAULT 'in_progress' CHECK(status IN ('in_progress', 'finished')),
            FOREIGN KEY (session_id) REFERENCES test_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (variant_id) REFERENCES variants(id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS session_variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            variant_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            FOREIGN KEY (session_id) REFERENCES test_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (variant_id) REFERENCES variants(id) ON DELETE CASCADE
        )
    ''')

    # Таблица ответов учеников
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            answer_1 INTEGER,
            answer_2 INTEGER,
            answer_text TEXT,
            is_correct BOOLEAN,
            answered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            upload_path TEXT,
            upload_name TEXT,
            upload_size INTEGER,
            upload_uploaded_at DATETIME,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )
    ''')

    # Таблица критериев оценки
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS grade_criteria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            total_tasks INTEGER NOT NULL,
            grade_5_min INTEGER NOT NULL,
            grade_4_min INTEGER NOT NULL,
            grade_3_min INTEGER NOT NULL
        )
    ''')

    # Таблица настроек
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    # Файлы .variant, которые уже загружены (по содержимому): «Загрузить новые из папки» их пропускает
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS variant_files (
            sha1 TEXT PRIMARY KEY,
            path TEXT,
            variant_id INTEGER,
            imported_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Добавляем критерии по умолчанию для полного варианта (27 задач)
    cursor.execute('SELECT COUNT(*) FROM grade_criteria WHERE total_tasks = 27')
    if cursor.fetchone()[0] == 0:
        cursor.execute('''
            INSERT INTO grade_criteria (name, total_tasks, grade_5_min, grade_4_min, grade_3_min)
            VALUES ('Полный вариант ЕГЭ', 27, 23, 18, 12)
        ''')

    conn.commit()
    conn.close()

# Функции для работы с задачами
class Task:
    @staticmethod
    def create(ege_number, image_path, answer_1=None, answer_count=1, answer_2=None,
               attachment_path=None, attachment_name=None, answer_text=None,
               task_scope='ege', class_id=None, answer_kind='classic'):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO tasks (ege_number, task_scope, class_id, image_path, attachment_path, attachment_name,
                             answer_kind, answer_count, answer_1, answer_2, answer_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (ege_number, task_scope, class_id, image_path, attachment_path, attachment_name,
              answer_kind, answer_count, answer_1, answer_2, answer_text))
        task_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return task_id

    @staticmethod
    def get_by_id(task_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM tasks WHERE id = ?', (task_id,))
        task = cursor.fetchone()
        conn.close()
        return dict(task) if task else None

    @staticmethod
    def get_attachment_name(attachment_path):
        """Настоящее имя файла к заданию (на диске он хранится под случайным именем)."""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT attachment_name FROM tasks WHERE attachment_path = ? LIMIT 1', (attachment_path,))
        row = cursor.fetchone()
        conn.close()
        return row['attachment_name'] if row else None

    @staticmethod
    def get_by_ege_number(ege_number):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE task_scope = 'ege' AND ege_number = ? ORDER BY created_at DESC",
                      (ege_number,))
        tasks = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return tasks

    @staticmethod
    def get_by_class_id(class_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE task_scope = 'class' AND class_id = ? ORDER BY created_at DESC", (class_id,))
        tasks = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return tasks

    @staticmethod
    def get_all(scope=None, class_id=None):
        conn = get_db()
        cursor = conn.cursor()
        if scope == 'ege':
            cursor.execute("SELECT * FROM tasks WHERE task_scope = 'ege' ORDER BY ege_number, created_at DESC")
        elif scope == 'class' and class_id is not None:
            cursor.execute("SELECT * FROM tasks WHERE task_scope = 'class' AND class_id = ? ORDER BY created_at DESC", (class_id,))
        elif scope == 'class':
            cursor.execute("SELECT * FROM tasks WHERE task_scope = 'class' ORDER BY created_at DESC")
        else:
            cursor.execute('SELECT * FROM tasks ORDER BY task_scope, ege_number, created_at DESC')
        tasks = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return tasks

    @staticmethod
    def count_by_ege_number():
        """Возвращает словарь {номер_ЕГЭ: количество_задач}"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT ege_number, COUNT(*) as count FROM tasks WHERE task_scope = 'ege' GROUP BY ege_number")
        result = {row['ege_number']: row['count'] for row in cursor.fetchall()}
        conn.close()
        return result

    @staticmethod
    def count_by_class():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT c.id as class_id, c.name as class_name, COUNT(t.id) as count
            FROM classes c
            LEFT JOIN tasks t ON t.class_id = c.id AND t.task_scope = 'class'
            GROUP BY c.id, c.name
            ORDER BY CASE WHEN c.name = 'Общий банк' THEN 0 ELSE 1 END, c.name
        ''')
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    @staticmethod
    def update(task_id, **kwargs):
        conn = get_db()
        cursor = conn.cursor()
        fields = []
        values = []
        for key, value in kwargs.items():
            if key in ['ege_number', 'task_scope', 'class_id', 'image_path', 'attachment_path', 'attachment_name',
                      'answer_kind', 'answer_count', 'answer_1', 'answer_2', 'answer_text']:
                fields.append(f'{key} = ?')
                values.append(value)
        if fields:
            values.append(task_id)
            cursor.execute(f'UPDATE tasks SET {", ".join(fields)} WHERE id = ?', values)
            conn.commit()
        conn.close()

    @staticmethod
    def delete(task_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
        conn.commit()
        conn.close()

    @staticmethod
    def move_to_class(task_ids, class_id):
        if not task_ids:
            return 0
        conn = get_db()
        cursor = conn.cursor()
        placeholders = ','.join('?' for _ in task_ids)
        params = [class_id] + list(task_ids)
        cursor.execute(
            f"UPDATE tasks SET task_scope = 'class', class_id = ? WHERE id IN ({placeholders})",
            params,
        )
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected


class ClassGroup:
    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM classes ORDER BY CASE WHEN name = 'Общий банк' THEN 0 ELSE 1 END, name")
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    @staticmethod
    def get_default_id():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO classes (name) VALUES ('Общий банк')")
        cursor.execute("SELECT id FROM classes WHERE name = 'Общий банк' LIMIT 1")
        row = cursor.fetchone()
        conn.commit()
        conn.close()
        return int(row['id']) if row else None

    @staticmethod
    def get_by_id(class_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM classes WHERE id = ?', (class_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def create(name):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO classes (name) VALUES (?)', (name.strip(),))
        class_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return class_id

# Функции для работы с вариантами
class Variant:
    @staticmethod
    def create(name, variant_type, ege_number=None, variant_scope='ege', class_id=None):
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO variants (name, variant_type, ege_number, variant_scope, class_id)
                VALUES (?, ?, ?, ?, ?)
            ''', (name, variant_type, ege_number, variant_scope, class_id))
            variant_id = cursor.lastrowid
            conn.commit()
            return variant_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def set_catalog(variant_id, grade=None, topic=None, kit=None, minutes=None):
        """Место варианта в каталоге. None — поле не менять; '' — очистить."""
        import catalog
        fields = {}
        if grade is not None:
            fields['grade'] = catalog.grade_label(grade) or None
        if topic is not None:
            fields['topic'] = catalog.clean_field(topic) or None
        if kit is not None:
            fields['kit'] = catalog.clean_field(kit) or None
        if minutes is not None:
            fields['minutes'] = catalog.clean_minutes(minutes)
        if not fields:
            return
        conn = get_db()
        try:
            conn.execute(f"UPDATE variants SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                         (*fields.values(), variant_id))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def results_stats():
        """{variant_id: {'runs': сколько учеников писали, 'percent': средний процент верных}} по завершённым тестированиям."""
        conn = get_db()
        try:
            rows = conn.execute('''
                SELECT s.variant_id,
                       (SELECT COUNT(*) FROM answers a
                         WHERE a.student_id = s.id AND a.is_correct = 1
                           AND a.task_id IN (SELECT task_id FROM variant_tasks vt WHERE vt.variant_id = s.variant_id)) AS ok,
                       (SELECT COUNT(*) FROM variant_tasks vt WHERE vt.variant_id = s.variant_id) AS total
                FROM students s JOIN test_sessions t ON t.id = s.session_id
                WHERE t.status = 'closed' ''').fetchall()
        finally:
            conn.close()
        stats = {}
        for row in rows:
            if not row['total']:
                continue
            item = stats.setdefault(row['variant_id'], {'runs': 0, 'sum': 0.0})
            item['runs'] += 1
            item['sum'] += row['ok'] / row['total']
        return {vid: {'runs': s['runs'], 'percent': round(100 * s['sum'] / s['runs'])} for vid, s in stats.items()}

    @staticmethod
    def add_tasks(variant_id, task_ids):
        """Добавляет задачи в вариант"""
        conn = get_db()
        try:
            cursor = conn.cursor()
            rows = [(variant_id, task_id, position) for position, task_id in enumerate(task_ids, 1)]
            cursor.executemany('''
                INSERT INTO variant_tasks (variant_id, task_id, position)
                VALUES (?, ?, ?)
            ''', rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def update(variant_id, **kwargs):
        conn = get_db()
        try:
            cursor = conn.cursor()
            fields = []
            values = []
            for key, value in kwargs.items():
                if key in ['name', 'variant_type', 'ege_number', 'variant_scope', 'class_id']:
                    fields.append(f'{key} = ?')
                    values.append(value)
            if fields:
                values.append(variant_id)
                cursor.execute(f'UPDATE variants SET {", ".join(fields)} WHERE id = ?', values)
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def replace_tasks(variant_id, task_ids):
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM variant_tasks WHERE variant_id = ?', (variant_id,))
            if task_ids:
                rows = [(variant_id, task_id, position) for position, task_id in enumerate(task_ids, 1)]
                cursor.executemany('''
                    INSERT INTO variant_tasks (variant_id, task_id, position)
                    VALUES (?, ?, ?)
                ''', rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def get_by_id(variant_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM variants WHERE id = ?', (variant_id,))
        variant = cursor.fetchone()
        conn.close()
        return dict(variant) if variant else None

    @staticmethod
    def get_tasks(variant_id):
        """Получить задачи варианта"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT t.*, vt.position FROM tasks t
            JOIN variant_tasks vt ON t.id = vt.task_id
            WHERE vt.variant_id = ?
            ORDER BY vt.position
        ''', (variant_id,))
        tasks = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return tasks

    @staticmethod
    def get_all(scope=None):
        conn = get_db()
        cursor = conn.cursor()
        if scope in ('ege', 'class'):
            cursor.execute('SELECT * FROM variants WHERE variant_scope = ? ORDER BY created_at DESC', (scope,))
        else:
            cursor.execute('SELECT * FROM variants ORDER BY created_at DESC')
        variants = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return variants

    @staticmethod
    def delete(variant_id, cascade=False):
        conn = get_db()
        try:
            cursor = conn.cursor()
            if not cascade:
                cursor.execute('SELECT 1 FROM test_sessions WHERE variant_id = ? LIMIT 1', (variant_id,))
                if cursor.fetchone():
                    return False
                cursor.execute('SELECT 1 FROM session_variants WHERE variant_id = ? LIMIT 1', (variant_id,))
                if cursor.fetchone():
                    return False
                cursor.execute('SELECT 1 FROM students WHERE variant_id = ? LIMIT 1', (variant_id,))
                if cursor.fetchone():
                    return False
                cursor.execute('DELETE FROM variants WHERE id = ?', (variant_id,))
                conn.commit()
                return True

            # Cascade delete: remove all results and sessions linked to the variant
            cursor.execute('''
                DELETE FROM answers
                WHERE student_id IN (
                    SELECT id FROM students WHERE variant_id = ?
                    UNION
                    SELECT st.id
                    FROM students st
                    JOIN session_variants sv ON sv.session_id = st.session_id
                    WHERE sv.variant_id = ?
                )
            ''', (variant_id, variant_id))
            cursor.execute('''
                DELETE FROM students
                WHERE session_id IN (SELECT session_id FROM session_variants WHERE variant_id = ?)
            ''', (variant_id,))
            cursor.execute('DELETE FROM students WHERE variant_id = ?', (variant_id,))
            cursor.execute('DELETE FROM test_sessions WHERE id IN (SELECT session_id FROM session_variants WHERE variant_id = ?)', (variant_id,))
            cursor.execute('DELETE FROM session_variants WHERE variant_id = ?', (variant_id,))
            cursor.execute('DELETE FROM test_sessions WHERE variant_id = ?', (variant_id,))
            cursor.execute('DELETE FROM variant_tasks WHERE variant_id = ?', (variant_id,))
            cursor.execute('DELETE FROM variants WHERE id = ?', (variant_id,))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

# Функции для работы с критериями оценки
class GradeCriteria:
    @staticmethod
    def get_for_total(total_tasks):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM grade_criteria WHERE total_tasks = ?', (total_tasks,))
        criteria = cursor.fetchone()
        conn.close()
        return dict(criteria) if criteria else None

    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM grade_criteria ORDER BY total_tasks')
        criteria = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return criteria

    @staticmethod
    def create_or_update(name, total_tasks, grade_5_min, grade_4_min, grade_3_min):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM grade_criteria WHERE total_tasks = ?', (total_tasks,))
        existing = cursor.fetchone()
        if existing:
            cursor.execute('''
                UPDATE grade_criteria
                SET name = ?, grade_5_min = ?, grade_4_min = ?, grade_3_min = ?
                WHERE total_tasks = ?
            ''', (name, grade_5_min, grade_4_min, grade_3_min, total_tasks))
        else:
            cursor.execute('''
                INSERT INTO grade_criteria (name, total_tasks, grade_5_min, grade_4_min, grade_3_min)
                VALUES (?, ?, ?, ?, ?)
            ''', (name, total_tasks, grade_5_min, grade_4_min, grade_3_min))
        conn.commit()
        conn.close()

    @staticmethod
    def calculate_grade(correct_count, total_tasks, session_criteria=None):
        """Вычислить оценку по количеству правильных ответов

        session_criteria - словарь с полями grade_5_min, grade_4_min, grade_3_min
                          от конкретной сессии тестирования (если заданы)
        """
        # Если переданы критерии сессии - используем их
        if session_criteria and session_criteria.get('grade_5_min') is not None:
            if correct_count >= session_criteria['grade_5_min']:
                return 5
            elif correct_count >= session_criteria['grade_4_min']:
                return 4
            elif correct_count >= session_criteria['grade_3_min']:
                return 3
            else:
                return 2

        # Иначе ищем общие критерии
        criteria = GradeCriteria.get_for_total(total_tasks)
        if not criteria:
            # Если нет критериев для данного количества задач, используем пропорцию
            percent = correct_count / total_tasks * 100 if total_tasks > 0 else 0
            if percent >= 85:
                return 5
            elif percent >= 65:
                return 4
            elif percent >= 45:
                return 3
            else:
                return 2

        if correct_count >= criteria['grade_5_min']:
            return 5
        elif correct_count >= criteria['grade_4_min']:
            return 4
        elif correct_count >= criteria['grade_3_min']:
            return 3
        else:
            return 2

if __name__ == '__main__':
    init_db()
    print('База данных инициализирована')

# Функции для работы с тестированиями (сессиями)
class TestSession:
    @staticmethod
    def create(variant_id, individual_mode, time_limit, access_code, show_answers, teacher_finish_only, calculator_enabled, python_enabled,
               thematic_ege_number=None, thematic_tasks_count=None,
               grade_5_min=None, grade_4_min=None, grade_3_min=None, total_tasks=None, shuffle_tasks=False, mask_answers=False):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO test_sessions (variant_id, individual_mode, time_limit,
                                       access_code, show_answers, teacher_finish_only, calculator_enabled, python_enabled, status,
                                       grade_5_min, grade_4_min, grade_3_min, total_tasks, shuffle_tasks, mask_answers)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
        ''', (variant_id, individual_mode, time_limit, access_code, show_answers, teacher_finish_only, calculator_enabled, python_enabled,
              grade_5_min, grade_4_min, grade_3_min, total_tasks, 1 if shuffle_tasks else 0, 1 if mask_answers else 0))
        session_id = cursor.lastrowid

        # Сохраняем настройки для индивидуальных вариантов
        if individual_mode and thematic_ege_number is not None:
            cursor.execute('''
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = ?
            ''', (f'session_{session_id}_ege', str(thematic_ege_number), str(thematic_ege_number)))
            cursor.execute('''
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = ?
            ''', (f'session_{session_id}_count', str(thematic_tasks_count), str(thematic_tasks_count)))

        conn.commit()
        conn.close()
        return session_id

    @staticmethod
    def get_by_id(session_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM test_sessions WHERE id = ?', (session_id,))
        session = cursor.fetchone()
        conn.close()
        return dict(session) if session else None

    @staticmethod
    def set_variants(session_id, variant_ids):
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM session_variants WHERE session_id = ?', (session_id,))
            rows = [
                (session_id, variant_id, position)
                for position, variant_id in enumerate(variant_ids, 1)
            ]
            if rows:
                cursor.executemany('''
                    INSERT INTO session_variants (session_id, variant_id, position)
                    VALUES (?, ?, ?)
                ''', rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def get_variants(session_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT sv.session_id, sv.variant_id, sv.position, v.name, v.variant_type,
                   v.variant_scope, v.class_id, v.created_at
            FROM session_variants sv
            JOIN variants v ON v.id = sv.variant_id
            WHERE sv.session_id = ?
            ORDER BY sv.position
        ''', (session_id,))
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    @staticmethod
    def get_variant_by_position(session_id, position):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT sv.session_id, sv.variant_id, sv.position, v.name, v.variant_type,
                   v.variant_scope, v.class_id, v.created_at
            FROM session_variants sv
            JOIN variants v ON v.id = sv.variant_id
            WHERE sv.session_id = ? AND sv.position = ?
            LIMIT 1
        ''', (session_id, position))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def get_active():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM test_sessions WHERE status = 'active' ORDER BY created_at DESC LIMIT 1")
        session = cursor.fetchone()
        conn.close()
        return dict(session) if session else None

    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM test_sessions ORDER BY created_at DESC')
        sessions = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return sessions

    @staticmethod
    def close(session_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE test_sessions SET status = 'closed' WHERE id = ?", (session_id,))
        conn.commit()
        conn.close()

    @staticmethod
    def pause(session_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE test_sessions
            SET paused = 1, paused_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'active' AND paused = 0
        ''', (session_id,))
        conn.commit()
        conn.close()

    @staticmethod
    def resume(session_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT paused, paused_at, pause_total_seconds FROM test_sessions WHERE id = ?', (session_id,))
        row = cursor.fetchone()
        if row and row['paused'] and row['paused_at']:
            try:
                paused_at = datetime.fromisoformat(row['paused_at'])
                delta = int((datetime.now(timezone.utc).replace(tzinfo=None) - paused_at).total_seconds())
            except Exception:
                delta = 0
            cursor.execute('''
                UPDATE test_sessions
                SET paused = 0, paused_at = NULL, pause_total_seconds = pause_total_seconds + ?
                WHERE id = ?
            ''', (delta, session_id))
        else:
            cursor.execute('UPDATE test_sessions SET paused = 0, paused_at = NULL WHERE id = ?', (session_id,))
        conn.commit()
        conn.close()

    @staticmethod
    def extend_time(session_id, extra_seconds):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE test_sessions
            SET extra_seconds = extra_seconds + ?
            WHERE id = ?
        ''', (extra_seconds, session_id))
        conn.commit()
        conn.close()

    @staticmethod
    def get_students(session_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM students WHERE session_id = ? ORDER BY started_at DESC
        ''', (session_id,))
        students = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return students

    @staticmethod
    def delete_with_results(session_id):
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM answers
                WHERE student_id IN (SELECT id FROM students WHERE session_id = ?)
            ''', (session_id,))
            cursor.execute('DELETE FROM students WHERE session_id = ?', (session_id,))
            cursor.execute('DELETE FROM test_sessions WHERE id = ?', (session_id,))
            conn.commit()
            return cursor.rowcount > 0
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class Student:
    @staticmethod
    def create(session_id, first_name, last_name, variant_id, variant_label=None):
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT paused, paused_at, pause_total_seconds FROM test_sessions WHERE id = ?', (session_id,))
            row = cursor.fetchone()
            pause_base = 0
            if row:
                pause_base = row['pause_total_seconds'] or 0
                if row['paused'] and row['paused_at']:
                    try:
                        paused_at = datetime.fromisoformat(row['paused_at'])
                        pause_base += max(0, int((datetime.now(timezone.utc).replace(tzinfo=None) - paused_at).total_seconds()))
                    except Exception:
                        pass
            cursor.execute('''
                INSERT INTO students (session_id, first_name, last_name, variant_id, variant_label, status, pause_base_seconds)
                VALUES (?, ?, ?, ?, ?, 'in_progress', ?)
            ''', (session_id, first_name, last_name, variant_id, variant_label, pause_base))
            student_id = cursor.lastrowid
            conn.commit()
            return student_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def get_by_id(student_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM students WHERE id = ?', (student_id,))
        student = cursor.fetchone()
        conn.close()
        return dict(student) if student else None

    @staticmethod
    def get_by_session_and_name(session_id, first_name, last_name):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM students
            WHERE session_id = ? AND first_name = ? AND last_name = ?
        ''', (session_id, first_name, last_name))
        student = cursor.fetchone()
        conn.close()
        return dict(student) if student else None

    @staticmethod
    def touch(student_id):
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE students SET last_seen_at = CURRENT_TIMESTAMP WHERE id = ?
            ''', (student_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def set_submitted(student_id, submitted):
        """«Работа сдана» (True) или «вернуть к решению» (False). Тест при этом не завершается."""
        conn = get_db()
        try:
            if submitted:
                conn.execute('''UPDATE students SET submitted_at = COALESCE(submitted_at, CURRENT_TIMESTAMP)
                                WHERE id = ? AND status = 'in_progress' ''', (student_id,))
            else:
                conn.execute('UPDATE students SET submitted_at = NULL WHERE id = ?', (student_id,))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def set_away(student_id, away):
        """Ученик ушёл со страницы (away=True) или вернулся. Время считает сервер по своим часам."""
        conn = get_db()
        try:
            cursor = conn.cursor()
            if away:
                # повторный «уход» без возвращения не считается ещё раз
                cursor.execute('''
                    UPDATE students SET leave_count = leave_count + 1, left_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND left_at IS NULL
                ''', (student_id,))
            else:
                cursor.execute('''
                    UPDATE students
                    SET leave_seconds = leave_seconds + MAX(0, CAST(strftime('%s','now') - strftime('%s', left_at) AS INTEGER)),
                        left_at = NULL
                    WHERE id = ? AND left_at IS NOT NULL
                ''', (student_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def finish(student_id):
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE students
                SET leave_seconds = leave_seconds + CASE WHEN left_at IS NULL THEN 0
                        ELSE MAX(0, CAST(strftime('%s','now') - strftime('%s', left_at) AS INTEGER)) END,
                    left_at = NULL,
                    status = 'finished', finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (student_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def finish_all(session_id):
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE students
                SET leave_seconds = leave_seconds + CASE WHEN left_at IS NULL THEN 0
                        ELSE MAX(0, CAST(strftime('%s','now') - strftime('%s', left_at) AS INTEGER)) END,
                    left_at = NULL,
                    status = 'finished', finished_at = CURRENT_TIMESTAMP
                WHERE session_id = ? AND status != 'finished'
            ''', (session_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def get_answers(student_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT a.*, t.ege_number, t.answer_1 as correct_1, t.answer_2 as correct_2,
                   t.answer_count, t.image_path
            FROM answers a
            JOIN tasks t ON a.task_id = t.id
            JOIN students s ON s.id = a.student_id
            WHERE a.student_id = ?
              AND a.task_id IN (SELECT task_id FROM variant_tasks WHERE variant_id = s.variant_id)
            ORDER BY t.ege_number
        ''', (student_id,))
        answers = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return answers


# Русские буквы, которые выглядят как латинские. Ученик пишет «3В8» с русской «В», а в ответе латинская «B»:
# при проверке такие буквы считаются одинаковыми. Замена делается с обеих сторон, поэтому ответы
# русскими словами по-прежнему сравниваются верно.
_LOOKALIKES = str.maketrans({
    'А': 'A', 'В': 'B', 'С': 'C', 'Е': 'E', 'Н': 'H', 'К': 'K', 'М': 'M', 'О': 'O', 'Р': 'P', 'Т': 'T', 'Х': 'X', 'У': 'Y',
    'а': 'a', 'в': 'b', 'с': 'c', 'е': 'e', 'н': 'h', 'к': 'k', 'м': 'm', 'о': 'o', 'р': 'p', 'т': 't', 'х': 'x', 'у': 'y',
})


def unify_letters(text):
    return text.translate(_LOOKALIKES)


class Answer:
    @staticmethod
    def save(student_id, task_id, answer_1=None, answer_2=None, answer_text=None, touch=True):
        """touch=False — только перепроверить, не меняя время ответа."""
        conn = get_db()
        cursor = conn.cursor()

        def _norm_atom(v):
            if v is None:
                return None
            s = str(v).strip()
            if s == '':
                return None
            # Сжимаем пробелы внутри (часто вводят случайные пробелы)
            s = ' '.join(s.split())
            return unify_letters(s)

        def _is_int_like(s: str) -> bool:
            if s is None:
                return False
            if s.startswith(('+', '-')):
                return s[1:].isdigit() and len(s) > 1
            return s.isdigit()

        def _equal(a, b) -> bool:
            a = _norm_atom(a)
            b = _norm_atom(b)
            if a is None or b is None:
                return False
            # Если оба ответа выглядят как целые числа — сравниваем как числа
            if _is_int_like(a) and _is_int_like(b):
                try:
                    return int(a) == int(b)
                except Exception:
                    pass
            # Иначе — регистронезависимое сравнение (работает и для кириллицы)
            return a.casefold() == b.casefold()

        try:
            # Получаем правильные ответы
            cursor.execute('SELECT answer_1, answer_2, answer_count, answer_text FROM tasks WHERE id = ?', (task_id,))
            task = cursor.fetchone()

            # Проверяем правильность
            is_correct = None  # None = не проверено/неприменимо (например, текстовый ответ)
            if task:
                if task['answer_count'] == 0:
                    # Текстовый/многострочный ответ: сравниваем с эталоном, если он задан.
                    # Иначе оставляем None (можно проверить вручную).
                    correct_text = task['answer_text']
                    if correct_text is not None and str(correct_text).strip() != '':
                        def norm(s: str) -> str:
                            return unify_letters('\n'.join(' '.join(line.strip().split()) for line in str(s).strip().splitlines() if line.strip() != ''))
                        is_correct = (norm(answer_text or '') == norm(correct_text))
                    else:
                        is_correct = None
                elif task['answer_count'] == 1:
                    is_correct = _equal(answer_1, task['answer_1'])
                elif task['answer_count'] == 2:
                    is_correct = (_equal(answer_1, task['answer_1']) and _equal(answer_2, task['answer_2']))
                else:
                    count = int(task['answer_count'])
                    expected = [task['answer_1'], task['answer_2']]
                    extra_expected = [line.strip() for line in str(task['answer_text'] or '').splitlines()]
                    for idx in range(2, count):
                        expected.append(extra_expected[idx - 2] if idx - 2 < len(extra_expected) else None)

                    provided = [answer_1, answer_2]
                    extra_provided = [line.strip() for line in str(answer_text or '').splitlines()]
                    for idx in range(2, count):
                        provided.append(extra_provided[idx - 2] if idx - 2 < len(extra_provided) else None)

                    is_correct = all(_equal(provided[i], expected[i]) for i in range(count))

            # Сохраняем или обновляем ответ
            cursor.execute('''
                INSERT INTO answers (student_id, task_id, answer_1, answer_2, answer_text, is_correct)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(student_id, task_id) DO UPDATE SET
                    answer_1 = excluded.answer_1, answer_2 = excluded.answer_2,
                    answer_text = excluded.answer_text, is_correct = excluded.is_correct,
                    answered_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE answers.answered_at END
            ''', (student_id, task_id, answer_1, answer_2, answer_text, is_correct, 1 if touch else 0))

            conn.commit()
            return is_correct
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def get_for_student_task(student_id, task_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM answers WHERE student_id = ? AND task_id = ?
        ''', (student_id, task_id))
        answer = cursor.fetchone()
        conn.close()
        return dict(answer) if answer else None

    @staticmethod
    def save_upload(student_id, task_id, upload_path, upload_name, upload_size):
        """Сохранить/обновить загруженный учеником файл (file_upload задача)."""
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO answers
                    (student_id, task_id, upload_path, upload_name, upload_size,
                     upload_uploaded_at, is_correct)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, NULL)
                ON CONFLICT(student_id, task_id) DO UPDATE SET
                    upload_path = excluded.upload_path, upload_name = excluded.upload_name,
                    upload_size = excluded.upload_size, upload_uploaded_at = CURRENT_TIMESTAMP,
                    is_correct = NULL, answered_at = CURRENT_TIMESTAMP
            ''', (student_id, task_id, upload_path, upload_name, upload_size))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def count_uploads_for_student(student_id):
        """Вернуть количество загруженных файлов для ученика (upload_path IS NOT NULL)."""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT COUNT(*) FROM answers WHERE student_id = ? AND upload_path IS NOT NULL',
            (student_id,)
        )
        count = cursor.fetchone()[0]
        conn.close()
        return count

    @staticmethod
    def mark(student_id, task_id, is_correct):
        """Учитель вручную выставляет ✓/✗ для file_upload задачи."""
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO answers (student_id, task_id, is_correct)
                VALUES (?, ?, ?)
                ON CONFLICT(student_id, task_id) DO UPDATE SET is_correct = excluded.is_correct
            ''', (student_id, task_id, is_correct))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ==================== НАСТРОЙКИ, ПЕРЕПРОВЕРКА, КОПИИ БАЗЫ ====================

def get_setting(key, default=None):
    conn = get_db()
    try:
        row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    finally:
        conn.close()
    return row['value'] if row and row['value'] is not None else default


def set_setting(key, value):
    conn = get_db()
    try:
        conn.execute('INSERT INTO settings (key, value) VALUES (?, ?) '
                     'ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, None if value is None else str(value)))
        conn.commit()
    finally:
        conn.close()


def remember_variant_file(sha1, path, variant_id):
    conn = get_db()
    try:
        conn.execute('INSERT OR REPLACE INTO variant_files (sha1, path, variant_id) VALUES (?, ?, ?)', (sha1, path, variant_id))
        conn.commit()
    finally:
        conn.close()


def known_variant_files():
    """sha1 уже загруженных файлов .variant."""
    conn = get_db()
    try:
        return {row['sha1'] for row in conn.execute('SELECT sha1 FROM variant_files')}
    finally:
        conn.close()


def recheck_task_answers(task_id):
    """После исправления правильного ответа заново проверяет все сохранённые ответы на это задание.
    Ручные отметки учителя на заданиях с файлом не трогаются. Возвращает число перепроверенных ответов."""
    task = Task.get_by_id(task_id)
    if not task or task.get('answer_kind') == 'file_upload':
        return 0
    conn = get_db()
    try:
        rows = [dict(r) for r in conn.execute(
            'SELECT student_id, answer_1, answer_2, answer_text FROM answers WHERE task_id = ?', (task_id,))]
    finally:
        conn.close()
    for row in rows:
        Answer.save(row['student_id'], task_id, row['answer_1'], row['answer_2'], answer_text=row['answer_text'])
    return len(rows)


def autofill_catalog_once():
    """Один раз после обновления: раскладывает старые варианты по каталогу по их названиям.
    Учитель потом поправит, что не так. Возвращает число разложенных."""
    import catalog
    if get_setting('catalog_autofill_done') == '1':
        return 0
    conn = get_db()
    try:
        rows = [dict(r) for r in conn.execute('SELECT id, name FROM variants WHERE grade IS NULL AND topic IS NULL AND kit IS NULL')]
    finally:
        conn.close()
    for row in rows:
        row.update(catalog.guess(row['name']))
    catalog.join_kits(rows)
    for row in rows:
        Variant.set_catalog(row['id'], row['grade'], row['topic'], row['kit'], row['minutes'] or '')
    set_setting('catalog_autofill_done', '1')
    return len(rows)


def recheck_wrong_answers_once():
    """Один раз после обновления: перепроверяет ответы, засчитанные неверными (например, «3В8» с русской «В»).
    Время ответа и ручные отметки учителя на заданиях с файлом не трогаются. Возвращает число исправленных."""
    if get_setting('lookalike_recheck_done') == '1':
        return 0
    conn = get_db()
    try:
        rows = [dict(r) for r in conn.execute('''
            SELECT a.student_id, a.task_id, a.answer_1, a.answer_2, a.answer_text
            FROM answers a JOIN tasks t ON t.id = a.task_id
            WHERE a.is_correct = 0 AND COALESCE(t.answer_kind, '') != 'file_upload' ''')]
    finally:
        conn.close()
    fixed = sum(1 for row in rows if Answer.save(row['student_id'], row['task_id'], row['answer_1'], row['answer_2'],
                                                 answer_text=row['answer_text'], touch=False))
    set_setting('lookalike_recheck_done', '1')
    return fixed


def backup_database(folder, keep=20):
    """Копия базы (безопасно даже во время работы) в folder; хранятся последние keep копий. Возвращает путь."""
    import os
    import sqlite3
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    path = os.path.join(folder, f'database-{stamp}.db')
    number = 2
    while os.path.exists(path):
        path = os.path.join(folder, f'database-{stamp}-{number}.db')
        number += 1
    source = sqlite3.connect(DATABASE_PATH)
    target = sqlite3.connect(path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    copies = sorted(name for name in os.listdir(folder) if name.startswith('database-') and name.endswith('.db'))
    for name in copies[:-keep] if keep else []:
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            pass
    return path


def list_backups(folder):
    import os
    if not os.path.isdir(folder):
        return []
    items = []
    for name in sorted(os.listdir(folder), reverse=True):
        if name.startswith('database-') and name.endswith('.db'):
            full = os.path.join(folder, name)
            items.append({'name': name, 'size': os.path.getsize(full), 'mtime': os.path.getmtime(full)})
    return items


def variant_usage_counts():
    """{variant_id: сколько раз вариант проводился (одним вариантом или среди нескольких)}."""
    conn = get_db()
    try:
        rows = conn.execute('''
            SELECT variant_id, COUNT(*) AS n FROM (
                SELECT variant_id FROM test_sessions WHERE variant_id IS NOT NULL
                UNION ALL
                SELECT variant_id FROM session_variants
            ) GROUP BY variant_id''').fetchall()
    finally:
        conn.close()
    return {row['variant_id']: row['n'] for row in rows}


def remove_task_from_variant(variant_id, task_id):
    conn = get_db()
    try:
        conn.execute('DELETE FROM variant_tasks WHERE variant_id = ? AND task_id = ?', (variant_id, task_id))
        rows = conn.execute('SELECT id FROM variant_tasks WHERE variant_id = ? ORDER BY position', (variant_id,)).fetchall()
        for position, row in enumerate(rows, 1):
            conn.execute('UPDATE variant_tasks SET position = ? WHERE id = ?', (position, row['id']))
        conn.commit()
    finally:
        conn.close()
