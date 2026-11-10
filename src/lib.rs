pub mod agent;
pub mod bus;
pub mod cactus;
pub mod coin;
pub mod config;
pub mod memory;
pub mod models;
pub mod operate;
pub mod proactive;
pub mod skills;
pub mod system1;
pub mod tools;
pub mod ui;

/// The HTTP agent for `url`: through the environment's proxy, unless NO_PROXY exempts the host.
/// ureq ignores NO_PROXY on its own, which would send a local model server's traffic to the proxy.
pub fn http(url: &str) -> &'static ureq::Agent {
    static PROXIED: std::sync::OnceLock<ureq::Agent> = std::sync::OnceLock::new();
    static DIRECT: std::sync::OnceLock<ureq::Agent> = std::sync::OnceLock::new();
    let build = |proxy: bool| {
        ureq::AgentBuilder::new()
            .try_proxy_from_env(proxy)
            .timeout_connect(std::time::Duration::from_secs(15))
            .timeout_read(std::time::Duration::from_secs(600))
            .build()
    };
    let host = url.split("://").nth(1).unwrap_or(url).split(['/', '?']).next().unwrap_or("");
    let host = host.rsplit_once('@').map_or(host, |(_, h)| h);
    let host = if host.starts_with('[') { host.split(']').next().unwrap_or("").trim_start_matches('[') } else { host.split(':').next().unwrap_or("") };
    let no_proxy = std::env::var("NO_PROXY").or_else(|_| std::env::var("no_proxy")).unwrap_or_default();
    if bypass(host, &no_proxy) {
        DIRECT.get_or_init(|| build(false))
    } else {
        PROXIED.get_or_init(|| build(true))
    }
}

/// Whether NO_PROXY exempts `host`. Loopback is always exempt.
pub fn bypass(host: &str, no_proxy: &str) -> bool {
    let host = host.to_ascii_lowercase();
    if host == "localhost" || host == "::1" || host.starts_with("127.") {
        return true;
    }
    let ip: Option<std::net::Ipv4Addr> = host.parse().ok();
    no_proxy.split(',').map(str::trim).filter(|e| !e.is_empty()).any(|entry| {
        if entry == "*" {
            return true;
        }
        if let (Some(ip), Some((net, bits))) = (ip, entry.split_once('/')) {
            if let (Ok(net), Ok(bits)) = (net.parse::<std::net::Ipv4Addr>(), bits.parse::<u32>()) {
                let mask = if bits == 0 { 0 } else { u32::MAX << (32 - bits.min(32)) };
                return u32::from(ip) & mask == u32::from(net) & mask;
            }
        }
        let suffix = entry.trim_start_matches('*').trim_start_matches('.').to_ascii_lowercase();
        host == suffix || host.ends_with(&format!(".{suffix}"))
    })
}

#[cfg(test)]
mod tests {
    #[test]
    fn no_proxy_matching() {
        use super::bypass;
        let rules = "example.com,.svc.local,*.corp.net,10.0.0.0/8,192.168.1.5";
        for yes in ["localhost", "127.0.0.1", "::1", "example.com", "api.example.com", "a.svc.local", "x.corp.net", "10.2.3.4", "192.168.1.5"] {
            assert!(bypass(yes, rules), "{yes}");
        }
        for no in ["notexample.com", "huggingface.co", "11.0.0.1", "192.168.1.6", "corp.net.evil.io"] {
            assert!(!bypass(no, rules), "{no}");
        }
        assert!(bypass("anything", "*"));
    }
}
