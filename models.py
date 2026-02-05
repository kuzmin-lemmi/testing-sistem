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

    conn.commit()
    conn.close()


def init_db():
    """Инициализация базы данных"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Таблица задач
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ege_number INTEGER NOT NULL CHECK(ege_number >= 1 AND ege_number <= 27),
            image_path TEXT NOT NULL,
            attachment_path TEXT,
            attachment_name TEXT,
            answer_count INTEGER NOT NULL DEFAULT 1,
            answer_1 INTEGER,
            answer_2 INTEGER,
            answer_text TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Таблица вариантов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            variant_type TEXT NOT NULL CHECK(variant_type IN ('thematic', 'full', 'mixed', 'uploaded')),
            ege_number INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
            started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            finished_at DATETIME,
            status TEXT NOT NULL DEFAULT 'in_progress' CHECK(status IN ('in_progress', 'finished')),
            FOREIGN KEY (session_id) REFERENCES test_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (variant_id) REFERENCES variants(id)
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
            is_correct BOOLEAN,
            answered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
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
               attachment_path=None, attachment_name=None, answer_text=None):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO tasks (ege_number, image_path, attachment_path, attachment_name,
                             answer_count, answer_1, answer_2, answer_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (ege_number, image_path, attachment_path, attachment_name,
              answer_count, answer_1, answer_2, answer_text))
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
    def get_by_ege_number(ege_number):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM tasks WHERE ege_number = ? ORDER BY created_at DESC', 
                      (ege_number,))
        tasks = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return tasks
    
    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM tasks ORDER BY ege_number, created_at DESC')
        tasks = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return tasks
    
    @staticmethod
    def count_by_ege_number():
        """Возвращает словарь {номер_ЕГЭ: количество_задач}"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT ege_number, COUNT(*) as count FROM tasks GROUP BY ege_number')
        result = {row['ege_number']: row['count'] for row in cursor.fetchall()}
        conn.close()
        return result
    
    @staticmethod
    def update(task_id, **kwargs):
        conn = get_db()
        cursor = conn.cursor()
        fields = []
        values = []
        for key, value in kwargs.items():
            if key in ['ege_number', 'image_path', 'attachment_path', 'attachment_name',
                      'answer_count', 'answer_1', 'answer_2', 'answer_text']:
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

# Функции для работы с вариантами
class Variant:
    @staticmethod
    def create(name, variant_type, ege_number=None):
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO variants (name, variant_type, ege_number)
                VALUES (?, ?, ?)
            ''', (name, variant_type, ege_number))
            variant_id = cursor.lastrowid
            conn.commit()
            return variant_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
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
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
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
                cursor.execute('SELECT 1 FROM students WHERE variant_id = ? LIMIT 1', (variant_id,))
                if cursor.fetchone():
                    return False
                cursor.execute('DELETE FROM variants WHERE id = ?', (variant_id,))
                conn.commit()
                return True

            # Cascade delete: remove all results and sessions linked to the variant
            cursor.execute('''
                DELETE FROM answers
                WHERE student_id IN (SELECT id FROM students WHERE variant_id = ?)
            ''', (variant_id,))
            cursor.execute('DELETE FROM students WHERE variant_id = ?', (variant_id,))
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
    def create(variant_id, individual_mode, time_limit, access_code, show_answers, teacher_finish_only, calculator_enabled,
               thematic_ege_number=None, thematic_tasks_count=None,
               grade_5_min=None, grade_4_min=None, grade_3_min=None, total_tasks=None):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO test_sessions (variant_id, individual_mode, time_limit, 
                                       access_code, show_answers, teacher_finish_only, calculator_enabled, status,
                                       grade_5_min, grade_4_min, grade_3_min, total_tasks)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
        ''', (variant_id, individual_mode, time_limit, access_code, show_answers, teacher_finish_only, calculator_enabled,
              grade_5_min, grade_4_min, grade_3_min, total_tasks))
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


class Student:
    @staticmethod
    def create(session_id, first_name, last_name, variant_id):
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO students (session_id, first_name, last_name, variant_id, status)
                VALUES (?, ?, ?, ?, 'in_progress')
            ''', (session_id, first_name, last_name, variant_id))
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
    def finish(student_id):
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE students 
                SET status = 'finished', finished_at = CURRENT_TIMESTAMP 
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
                SET status = 'finished', finished_at = CURRENT_TIMESTAMP
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
            WHERE a.student_id = ?
            ORDER BY t.ege_number
        ''', (student_id,))
        answers = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return answers


class Answer:
    @staticmethod
    def save(student_id, task_id, answer_1=None, answer_2=None, answer_text=None):
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
            return s

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
            cursor.execute('SELECT answer_1, answer_2, answer_count FROM tasks WHERE id = ?', (task_id,))
            task = cursor.fetchone()
            
            # Проверяем правильность
            is_correct = None  # None = не проверено/неприменимо (например, текстовый ответ)
            if task:
                if task['answer_count'] == 0:
                    # Текстовый/многострочный ответ: сравниваем с эталоном, если он задан.
                    # Иначе оставляем None (можно проверить вручную).
                    cursor.execute('SELECT answer_text FROM tasks WHERE id = ?', (task_id,))
                    t2 = cursor.fetchone()
                    correct_text = (t2['answer_text'] if t2 else None)
                    if correct_text is not None and str(correct_text).strip() != '':
                        def norm(s: str) -> str:
                            return '\n'.join(' '.join(line.strip().split()) for line in str(s).strip().splitlines() if line.strip() != '')
                        is_correct = (norm(answer_text or '') == norm(correct_text))
                    else:
                        is_correct = None
                elif task['answer_count'] == 1:
                    is_correct = _equal(answer_1, task['answer_1'])
                else:
                    is_correct = (_equal(answer_1, task['answer_1']) and _equal(answer_2, task['answer_2']))

            # Сохраняем или обновляем ответ
            cursor.execute('SELECT id FROM answers WHERE student_id = ? AND task_id = ?', 
                          (student_id, task_id))
            existing = cursor.fetchone()
            
            if existing:
                cursor.execute('''
                    UPDATE answers SET answer_1 = ?, answer_2 = ?, answer_text = ?, is_correct = ?,
                                       answered_at = CURRENT_TIMESTAMP
                    WHERE student_id = ? AND task_id = ?
                ''', (answer_1, answer_2, answer_text, is_correct, student_id, task_id))
            else:
                cursor.execute('''
                    INSERT INTO answers (student_id, task_id, answer_1, answer_2, answer_text, is_correct)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (student_id, task_id, answer_1, answer_2, answer_text, is_correct))
            
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
