from django.urls import path

from checker import views

urlpatterns = [
    path("", views.index, name="index"),
    path("leaderboard", views.leaderboard, name="leaderboard"),
    path("reports", views.reports, name="reports"),
    path("api/eligibility", views.eligibility, name="eligibility"),
    path("api/topdps", views.top_dps, name="top_dps"),
    path("api/leaderboard", views.api_leaderboard, name="api_leaderboard"),
    path("api/reportcard", views.api_report_card, name="api_report_card"),
    path("api/items", views.item_search, name="item_search"),
    path("api/characters", views.character_search, name="character_search"),
]
