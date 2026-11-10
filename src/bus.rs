//! Agent-to-agent mail. One SQLite file, so agents in different processes can talk.

use anyhow::Result;
use rusqlite::{params, Connection};
use std::path::{Path, PathBuf};

pub struct Bus {
    path: PathBuf,
}

impl Bus {
    pub fn open(home: &Path) -> Result<Bus> {
        std::fs::create_dir_all(home)?;
        let bus = Bus { path: home.join("bus.db") };
        bus.db()?.execute_batch(
            "CREATE TABLE IF NOT EXISTS mail(id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, sender TEXT NOT NULL,
                 recipient TEXT NOT NULL, body TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0)",
        )?;
        Ok(bus)
    }

    fn db(&self) -> Result<Connection> {
        let db = Connection::open(&self.path)?;
        db.busy_timeout(std::time::Duration::from_secs(10))?;
        Ok(db)
    }

    pub fn send(&self, sender: &str, recipient: &str, body: &str) -> Result<()> {
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_secs() as i64;
        self.db()?.execute("INSERT INTO mail(ts, sender, recipient, body) VALUES (?, ?, ?, ?)", params![now, sender, recipient, body])?;
        Ok(())
    }

    /// Unread (sender, body) pairs for `name`, marked read in the same transaction.
    pub fn inbox(&self, name: &str) -> Result<Vec<(String, String)>> {
        let mut db = self.db()?;
        // Immediate: two readers must not both take the same mail.
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let mail: Vec<(String, String)> = tx
            .prepare("SELECT sender, body FROM mail WHERE recipient = ? AND read = 0 ORDER BY id")?
            .query_map([name], |r| Ok((r.get(0)?, r.get(1)?)))?
            .flatten()
            .collect();
        tx.execute("UPDATE mail SET read = 1 WHERE recipient = ? AND read = 0", [name])?;
        tx.commit()?;
        Ok(mail)
    }
}
