from django.urls import path

from . import views

app_name = 'library'

urlpatterns = [
    path('', views.library_view, name='library'),
    path('game/<int:pk>/', views.game_detail_view, name='game_detail'),
    path('settings/', views.settings_view, name='settings'),

    # HTMX/API endpoints
    path('api/games/add/', views.add_game, name='add_game'),
    path('api/games/<int:pk>/refresh/', views.refresh_metadata, name='refresh_metadata'),
    path('api/games/<int:pk>/set-folder/', views.set_install_folder, name='set_install_folder'),
    path('api/games/<int:pk>/set-exe/', views.set_executable, name='set_executable'),
    path('api/games/<int:pk>/launch/', views.launch_game, name='launch_game'),
    path('api/games/<int:pk>/mtool/', views.toggle_mtool, name='toggle_mtool'),
    path('api/games/<int:pk>/open-folder/', views.open_install_folder, name='open_install_folder'),
    path('api/games/<int:pk>/rating/', views.update_rating, name='update_rating'),
    path('api/games/<int:pk>/status/', views.update_status, name='update_status'),
    path('api/games/<int:pk>/delete/', views.delete_game, name='delete_game'),
    path('api/games/<int:pk>/card/', views.game_card_partial, name='game_card_partial'),
    path('api/games/<int:pk>/install-status/', views.install_status_partial, name='install_status_partial'),
    path('api/games/<int:pk>/tag-section/', views.tag_section_partial, name='tag_section_partial'),
    path('api/games/<int:pk>/tag-suggestions/', views.tag_suggestions, name='tag_suggestions'),
    path('api/games/<int:pk>/tags/add/', views.add_tag, name='add_tag'),
    path('api/games/<int:pk>/tags/remove/', views.remove_tag, name='remove_tag'),
    path('api/games/grid/', views.games_grid_partial, name='games_grid_partial'),
    path('api/pick-folder/', views.pick_folder_dialog, name='pick_folder'),
    path('api/pick-file/', views.pick_file_dialog, name='pick_file'),
    path('api/settings/save/', views.save_settings, name='save_settings'),
    path('api/games/<int:pk>/bad/', views.toggle_bad, name='toggle_bad'),
    path('api/games/<int:pk>/favorite/', views.toggle_favorite, name='toggle_favorite'),

    # Public API for browser extensions (CORS open, CSRF exempt).
    path('api/v1/lookup/', views.api_lookup, name='api_lookup'),
    path('api/v1/lookup-bulk/', views.api_lookup_bulk, name='api_lookup_bulk'),
    path('api/v1/upsert/', views.api_upsert, name='api_upsert'),
    path('api/v1/link/', views.api_link, name='api_link'),
    path('api/v1/health/', views.api_health, name='api_health'),
    path('api/changes/', views.api_changes, name='api_changes'),
]
