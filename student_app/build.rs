//! Значок и описание программы в .exe: значок у ярлыка «Тестирование», имя в диспетчере задач.
fn main() {
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        println!("cargo:rerun-if-changed=assets/icon.ico");
        let mut resource = winresource::WindowsResource::new();
        resource
            .set_icon("assets/icon.ico")
            .set("ProductName", "Тестирование")
            .set("FileDescription", "Тестирование")
            .set("CompanyName", "Тестирование");
        resource.compile().expect("ресурсы .exe (значок)");
    }
}
