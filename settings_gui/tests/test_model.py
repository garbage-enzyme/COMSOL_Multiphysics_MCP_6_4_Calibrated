def test_nested_unknown_key_is_reported_not_silently_dropped():
    from comsol_mcp.settings import default_settings_document
    from settings_gui.model import SettingsFormModel

    document = default_settings_document()
    document["runtime"]["future_option"] = True

    model = SettingsFormModel.from_raw(document)

    assert "runtime.future_option" in model.errors
    assert model.errors["runtime.future_option"] == "Unknown setting is not supported."
    assert model.canonical is None
